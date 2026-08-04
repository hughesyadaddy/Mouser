/*
 * mouser_hook.dll -- native WH_MOUSE_LL procedure for Mouser (Windows x64).
 *
 * Windows runs a low-level mouse hook procedure inside the hooking process
 * and blocks the whole system input pipeline until it returns, up to
 * LowLevelHooksTimeout (300ms). Mouser's procedure used to be Python, so it
 * had to take the GIL -- and every application's mouse input queued behind
 * whatever else Mouser's threads were doing. Measured on tiny11 under load:
 * SendInput 76-187ms with Mouser running, 1.3-1.6ms without.
 *
 * Everything here therefore runs without ever entering Python:
 *
 *   - the hook lives on a dedicated thread created inside this DLL, with its
 *     own message loop. It shares no thread with Python, so a Python thread
 *     holding the GIL can never delay hook delivery;
 *   - the procedure decides from two masks (interest, block) that Python
 *     pushes down whenever they change, plus a flags word. No callback, no
 *     allocation, no lock;
 *   - events Mouser acts on go into a lock-free single-producer /
 *     single-consumer ring. A Python drain thread collects them with the GIL
 *     released, off the input path entirely.
 *
 * Keep the struct layout, export names, and MOUSER_HOOK_ABI in step with
 * core/native_hook_filter.py and core/native_hook_win.py.
 *
 * Build: python native/win/build.py   (see native/win/README.md)
 */

#include <windows.h>

#define MOUSER_HOOK_ABI 2u

/* How long uninstall waits for the hook thread to come down. Must stay BELOW
 * HOOK_THREAD_JOIN_S in core/mouse_hook_windows.py, so Python never gives up
 * on a thread that is still running and drop its handle on it. */
#define NATIVE_UNINSTALL_WAIT_MS 2000u

#define EXPORT __declspec(dllexport)

/* ── filter flags: mirror core/native_hook_filter.py ─────────────────── */

#define FILTER_INTERCEPT       (1u << 0)
#define FILTER_VSCROLL_INVERT  (1u << 1)
#define FILTER_HSCROLL_INVERT  (1u << 2)
#define FILTER_DEBUG           (1u << 3)

/* ── event codes: mirror core/native_hook_filter.py ──────────────────── */

#define EVT_NONE            0u
#define EVT_XBUTTON1_DOWN   1u
#define EVT_XBUTTON1_UP     2u
#define EVT_XBUTTON2_DOWN   3u
#define EVT_XBUTTON2_UP     4u
#define EVT_MIDDLE_DOWN     5u
#define EVT_MIDDLE_UP       6u
#define EVT_HSCROLL_LEFT    7u
#define EVT_HSCROLL_RIGHT   8u

#define EVENT_BIT(code) (1u << (code))

/* Bits of the three DOWN events that pair with an UP. */
#define PAIRABLE_DOWN_BITS \
    (EVENT_BIT(EVT_XBUTTON1_DOWN) | EVENT_BIT(EVT_XBUTTON2_DOWN) | \
     EVENT_BIT(EVT_MIDDLE_DOWN))

/* Any wheel interest at all -- below this the wheel messages bail early. */
#define HSCROLL_INTEREST_BITS \
    (EVENT_BIT(EVT_HSCROLL_LEFT) | EVENT_BIT(EVT_HSCROLL_RIGHT))

/* WM_INPUT marks a Logitech wheel; a hook wheel message within this window
 * is attributed to it. Mirrors LOGITECH_SCROLL_RECENT_S (0.080s). */
#define LOGITECH_WHEEL_RECENT_MS 80ull

#define RING_SIZE 256u  /* power of two */
#define RING_MASK (RING_SIZE - 1u)

typedef struct {
    unsigned long long extra_info;
    unsigned int message;     /* the WM_* the hook saw */
    unsigned int mouse_data;
    unsigned int flags;       /* MSLLHOOKSTRUCT.flags */
    unsigned int event_code;  /* EVT_*; EVT_NONE for debug-only mirrors */
    unsigned int blocked;     /* 1 when the procedure swallowed it */
    unsigned int reserved;
} MouserHookEvent;

/* The Python side reads this struct straight out of the ring, so a compiler
 * that padded it would shift every field. Fail the build, not the machine. */
typedef char mouser_hook_event_layout_check[
    (sizeof(MouserHookEvent) == 32) ? 1 : -1];

/* ── state ───────────────────────────────────────────────────────────── */

static HINSTANCE g_hinst;

static HHOOK  g_hook;
static HANDLE g_thread;
static DWORD  g_thread_id;
static HANDLE g_ready;
static HANDLE g_sem;

/* flags, interest mask, and block mask packed into one word so the procedure
 * reads a combination Python actually pushed. Read as three separate volatiles
 * they could tear -- a new block mask paired with the old flags is a state no
 * compute_filter() result ever produced, and the procedure would act on it.
 * 16 bits each is ample: 4 flags, 9 event codes. */
#define FILTER_FLAGS_SHIFT    0
#define FILTER_INTEREST_SHIFT 16
#define FILTER_BLOCK_SHIFT    32
#define FILTER_FIELD_MASK     0xFFFFull

static volatile LONG64 g_filter;

/* Only the hook thread touches this: which swallowed DOWNs are still held. */
static unsigned int g_blocked_down_active;

static void *volatile g_inject_hwnd;
static volatile LONG g_inject_vscroll_msg;
static volatile LONG g_inject_hscroll_msg;
static volatile LONG g_pending_vscroll;
static volatile LONG g_pending_hscroll;
static volatile LONG g_vscroll_posted;
static volatile LONG g_hscroll_posted;

static volatile LONG64 g_last_logitech_wheel_ms;

static MouserHookEvent g_ring[RING_SIZE];
static volatile LONG g_ring_head;  /* producer: hook thread */
static volatile LONG g_ring_tail;  /* consumer: Python drain thread */
static volatile LONG g_dropped;

/* ── ring buffer (single producer, single consumer) ───────────────────── */

static void ring_push(const MouserHookEvent *event)
{
    LONG head = g_ring_head;
    LONG tail = g_ring_tail;

    if ((unsigned int)(head - tail) >= RING_SIZE) {
        InterlockedIncrement(&g_dropped);
        return;
    }
    g_ring[(unsigned int)head & RING_MASK] = *event;
    MemoryBarrier();
    g_ring_head = head + 1;
    ReleaseSemaphore(g_sem, 1, NULL);
}

/* ── helpers ─────────────────────────────────────────────────────────── */

static int hiword_signed(unsigned int dword)
{
    int value = (int)((dword >> 16) & 0xFFFFu);
    if (value >= 0x8000) {
        value -= 0x10000;
    }
    return value;
}

/* True when a Logitech wheel report arrived over WM_INPUT just now.
 *
 * The Python procedure used to also drain the thread's raw-input queue with
 * GetRawInputBuffer. That only worked because the hook shared a thread with
 * the raw-input window -- and it stole packets that window was about to get.
 * The hook has its own thread now, so the WM_INPUT mark
 * (mouser_hook_mark_logitech_wheel) is the whole attribution, which is the
 * path the old code fell back to for most strokes anyway. */
static BOOL wheel_is_logitech(void)
{
    LONG64 marked = InterlockedCompareExchange64(
        (volatile LONG64 *)&g_last_logitech_wheel_ms, 0, 0);
    if (marked == 0) {
        return FALSE;
    }
    return (LONG64)GetTickCount64() - marked <= (LONG64)LOGITECH_WHEEL_RECENT_MS;
}

/* Accumulate an inverted wheel delta and wake the injector window once.
 * Returns TRUE when the original event must be swallowed. */
static BOOL invert_wheel(BOOL vertical, int delta)
{
    volatile LONG *pending = vertical ? &g_pending_vscroll : &g_pending_hscroll;
    volatile LONG *posted  = vertical ? &g_vscroll_posted  : &g_hscroll_posted;
    LONG message = vertical ? g_inject_vscroll_msg : g_inject_hscroll_msg;
    HWND target = (HWND)InterlockedCompareExchangePointer(
        (void *volatile *)&g_inject_hwnd, NULL, NULL);

    if (delta == 0 || target == NULL || message == 0) {
        return FALSE;
    }
    InterlockedExchangeAdd(pending, -delta);
    if (InterlockedCompareExchange(posted, 1, 0) != 0) {
        /* An injection is already queued; it will pick this delta up too. */
        return TRUE;
    }
    if (PostMessageW(target, (UINT)message, 0, 0)) {
        return TRUE;
    }
    /* Could not queue the injection -- undo and let the original through
     * rather than swallowing a scroll that would never be replayed. */
    InterlockedExchange(posted, 0);
    InterlockedExchangeAdd(pending, delta);
    return FALSE;
}

/* Only swallow an UP whose DOWN we swallowed.
 *
 * The hook can be uninstalled between a physical press and its release.
 * Blocking an UP whose DOWN reached the OS leaves a stuck button; passing an
 * UP whose DOWN we swallowed is harmless. Mirrors _pair_blocked_updown. */
static BOOL pair_blocked_updown(unsigned int event_code, BOOL should_block)
{
    unsigned int down_code;

    switch (event_code) {
    case EVT_XBUTTON1_UP: down_code = EVT_XBUTTON1_DOWN; break;
    case EVT_XBUTTON2_UP: down_code = EVT_XBUTTON2_DOWN; break;
    case EVT_MIDDLE_UP:   down_code = EVT_MIDDLE_DOWN;   break;
    default:
        if (should_block && (EVENT_BIT(event_code) & PAIRABLE_DOWN_BITS)) {
            g_blocked_down_active |= EVENT_BIT(event_code);
        }
        return should_block;
    }

    {
        BOOL down_was_blocked =
            (g_blocked_down_active & EVENT_BIT(down_code)) != 0;
        g_blocked_down_active &= ~EVENT_BIT(down_code);
        return should_block && down_was_blocked;
    }
}

static unsigned int classify(UINT message, unsigned int mouse_data)
{
    switch (message) {
    case WM_XBUTTONDOWN:
        switch (hiword_signed(mouse_data)) {
        case XBUTTON1: return EVT_XBUTTON1_DOWN;
        case XBUTTON2: return EVT_XBUTTON2_DOWN;
        default:       return EVT_NONE;
        }
    case WM_XBUTTONUP:
        switch (hiword_signed(mouse_data)) {
        case XBUTTON1: return EVT_XBUTTON1_UP;
        case XBUTTON2: return EVT_XBUTTON2_UP;
        default:       return EVT_NONE;
        }
    case WM_MBUTTONDOWN:
        return EVT_MIDDLE_DOWN;
    case WM_MBUTTONUP:
        return EVT_MIDDLE_UP;
    case WM_MOUSEHWHEEL: {
        int delta = hiword_signed(mouse_data);
        if (delta > 0) return EVT_HSCROLL_LEFT;
        if (delta < 0) return EVT_HSCROLL_RIGHT;
        return EVT_NONE;
    }
    default:
        return EVT_NONE;
    }
}

static void queue_event(UINT message, const MSLLHOOKSTRUCT *data,
                        unsigned int event_code, BOOL blocked)
{
    MouserHookEvent event;
    event.extra_info = (unsigned long long)data->dwExtraInfo;
    event.message = (unsigned int)message;
    event.mouse_data = (unsigned int)data->mouseData;
    event.flags = (unsigned int)data->flags;
    event.event_code = event_code;
    event.blocked = blocked ? 1u : 0u;
    event.reserved = 0u;
    ring_push(&event);
}

/* ── the hook procedure ──────────────────────────────────────────────── */

static LRESULT CALLBACK ll_mouse_proc(int nCode, WPARAM wParam, LPARAM lParam)
{
    UINT message;
    unsigned int flags;
    unsigned int interest;
    unsigned int blocked_mask;
    unsigned int event_code;
    const MSLLHOOKSTRUCT *data;
    BOOL should_block;
    BOOL debug;

    if (nCode != HC_ACTION) {
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    message = (UINT)wParam;

    /* WM_MOUSEMOVE is the highest-frequency mouse event and Mouser never
     * remaps it -- gesture capture runs off the Raw Input path. Bail before
     * reading anything. */
    if (message == WM_MOUSEMOVE) {
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    {
        /* One read of the packed word: the three fields are guaranteed to
         * come from the same push. */
        LONG64 filter = InterlockedCompareExchange64(&g_filter, 0, 0);
        flags = (unsigned int)((filter >> FILTER_FLAGS_SHIFT) & FILTER_FIELD_MASK);
        interest = (unsigned int)((filter >> FILTER_INTEREST_SHIFT) & FILTER_FIELD_MASK);
        blocked_mask = (unsigned int)((filter >> FILTER_BLOCK_SHIFT) & FILTER_FIELD_MASK);
    }
    debug = (flags & FILTER_DEBUG) != 0;

    /* Wheel is the other high-frequency stream and a fast scroll delivers it
     * in bursts. Skip it unless something here would act on it. */
    if (message == WM_MOUSEWHEEL || message == WM_MOUSEHWHEEL) {
        BOOL wheel_work =
            (flags & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT)) != 0 ||
            ((flags & FILTER_INTERCEPT) != 0 &&
             (interest & HSCROLL_INTEREST_BITS) != 0);
        if (!wheel_work && !debug) {
            return CallNextHookEx(g_hook, nCode, wParam, lParam);
        }
    }

    data = (const MSLLHOOKSTRUCT *)lParam;
    if (data == NULL) {
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    /* Injected events are Deskflow relaying the far machine's mouse. Mouser
     * already handled that device on the machine it is attached to, and on a
     * KVM client they are the bulk of the stream. */
    if ((data->flags & LLMHF_INJECTED) != 0) {
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    if ((flags & FILTER_INTERCEPT) == 0) {
        /* No Logitech bound here, or KVM focus is on another machine. The
         * hook must be a pass-through for everything except the host-local
         * scroll invert, which stays armed while focus is remote. */
        if (message == WM_MOUSEWHEEL && (flags & FILTER_VSCROLL_INVERT) &&
            wheel_is_logitech()) {
            if (invert_wheel(TRUE, hiword_signed((unsigned int)data->mouseData))) {
                return 1;
            }
        } else if (message == WM_MOUSEHWHEEL &&
                   (flags & FILTER_HSCROLL_INVERT) && wheel_is_logitech()) {
            if (invert_wheel(FALSE, hiword_signed((unsigned int)data->mouseData))) {
                return 1;
            }
        }
        if (debug) {
            queue_event(message, data, EVT_NONE, FALSE);
        }
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    event_code = classify(message, (unsigned int)data->mouseData);

    /* Vertical wheel never dispatches an event -- inversion is all it does. */
    if (message == WM_MOUSEWHEEL) {
        if ((flags & FILTER_VSCROLL_INVERT) && wheel_is_logitech() &&
            invert_wheel(TRUE, hiword_signed((unsigned int)data->mouseData))) {
            return 1;
        }
        if (debug) {
            queue_event(message, data, EVT_NONE, FALSE);
        }
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    should_block = event_code != EVT_NONE &&
                   (blocked_mask & EVENT_BIT(event_code)) != 0;

    if (message == WM_MOUSEHWHEEL && !should_block) {
        /* Remapped horizontal scroll wins over inversion (it is swallowed
         * and replaced); otherwise invert it in place. */
        if ((flags & FILTER_HSCROLL_INVERT) && wheel_is_logitech() &&
            invert_wheel(FALSE, hiword_signed((unsigned int)data->mouseData))) {
            return 1;
        }
    }

    if (event_code == EVT_NONE) {
        if (debug) {
            queue_event(message, data, EVT_NONE, FALSE);
        }
        return CallNextHookEx(g_hook, nCode, wParam, lParam);
    }

    should_block = pair_blocked_updown(event_code, should_block);

    if ((interest & EVENT_BIT(event_code)) != 0 || debug) {
        queue_event(message, data, event_code, should_block);
    }
    if (should_block) {
        return 1;
    }
    return CallNextHookEx(g_hook, nCode, wParam, lParam);
}

/* ── hook thread ─────────────────────────────────────────────────────── */

static DWORD WINAPI hook_thread_main(LPVOID param)
{
    MSG msg;
    (void)param;

    g_hook = SetWindowsHookExW(WH_MOUSE_LL, ll_mouse_proc, g_hinst, 0);
    SetEvent(g_ready);

    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (g_hook != NULL) {
        UnhookWindowsHookEx(g_hook);
        g_hook = NULL;
    }
    return 0;
}

/* ── exports ─────────────────────────────────────────────────────────── */

EXPORT int mouser_hook_uninstall(void);

EXPORT unsigned int mouser_hook_abi_version(void)
{
    return MOUSER_HOOK_ABI;
}

EXPORT unsigned int mouser_hook_event_size(void)
{
    return (unsigned int)sizeof(MouserHookEvent);
}

EXPORT int mouser_hook_install(void)
{
    if (g_thread != NULL) {
        return g_hook != NULL ? 1 : 0;
    }
    if (g_sem == NULL) {
        g_sem = CreateSemaphoreW(NULL, 0, (LONG)RING_SIZE, NULL);
        if (g_sem == NULL) {
            return 0;
        }
    }
    g_ready = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (g_ready == NULL) {
        return 0;
    }
    g_blocked_down_active = 0u;
    g_thread = CreateThread(NULL, 0, hook_thread_main, NULL, 0, &g_thread_id);
    if (g_thread == NULL) {
        CloseHandle(g_ready);
        g_ready = NULL;
        return 0;
    }
    /* The hook procedure must outrun everything else on the machine; the
     * thread does nothing but run it. */
    SetThreadPriority(g_thread, THREAD_PRIORITY_TIME_CRITICAL);
    WaitForSingleObject(g_ready, 5000);
    CloseHandle(g_ready);
    g_ready = NULL;

    if (g_hook == NULL) {
        mouser_hook_uninstall();
        return 0;
    }
    return 1;
}

EXPORT int mouser_hook_uninstall(void)
{
    if (g_thread == NULL) {
        return 1;
    }
    PostThreadMessageW(g_thread_id, WM_QUIT, 0, 0);
    if (WaitForSingleObject(g_thread, NATIVE_UNINSTALL_WAIT_MS) != WAIT_OBJECT_0) {
        /* The thread is wedged; leaking its handle beats tearing the hook
         * out from under a procedure that may still be running. */
        return 0;
    }
    CloseHandle(g_thread);
    g_thread = NULL;
    g_thread_id = 0;
    g_blocked_down_active = 0u;
    return 1;
}

EXPORT void mouser_hook_set_filter(unsigned int flags,
                                   unsigned int interest_mask,
                                   unsigned int block_mask)
{
    LONG64 packed =
        ((LONG64)(flags & FILTER_FIELD_MASK) << FILTER_FLAGS_SHIFT) |
        ((LONG64)(interest_mask & FILTER_FIELD_MASK) << FILTER_INTEREST_SHIFT) |
        ((LONG64)(block_mask & FILTER_FIELD_MASK) << FILTER_BLOCK_SHIFT);
    InterlockedExchange64(&g_filter, packed);
}

EXPORT void mouser_hook_set_inject_target(void *hwnd,
                                          unsigned int vscroll_msg,
                                          unsigned int hscroll_msg)
{
    InterlockedExchange(&g_inject_vscroll_msg, (LONG)vscroll_msg);
    InterlockedExchange(&g_inject_hscroll_msg, (LONG)hscroll_msg);
    InterlockedExchangePointer((void *volatile *)&g_inject_hwnd, hwnd);
}

EXPORT void mouser_hook_mark_logitech_wheel(void)
{
    InterlockedExchange64((volatile LONG64 *)&g_last_logitech_wheel_ms,
                          (LONG64)GetTickCount64());
}

EXPORT int mouser_hook_next_event(MouserHookEvent *out, unsigned int timeout_ms)
{
    LONG tail;

    if (out == NULL || g_sem == NULL) {
        return 0;
    }
    if (WaitForSingleObject(g_sem, timeout_ms) != WAIT_OBJECT_0) {
        return 0;
    }
    tail = g_ring_tail;
    if (tail == g_ring_head) {
        return 0;
    }
    *out = g_ring[(unsigned int)tail & RING_MASK];
    MemoryBarrier();
    g_ring_tail = tail + 1;
    return 1;
}

EXPORT int mouser_hook_take_pending_vscroll(void)
{
    LONG delta = InterlockedExchange(&g_pending_vscroll, 0);
    InterlockedExchange(&g_vscroll_posted, 0);
    return (int)delta;
}

EXPORT int mouser_hook_take_pending_hscroll(void)
{
    LONG delta = InterlockedExchange(&g_pending_hscroll, 0);
    InterlockedExchange(&g_hscroll_posted, 0);
    return (int)delta;
}

EXPORT unsigned int mouser_hook_dropped(void)
{
    return (unsigned int)g_dropped;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_hinst = instance;
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}
