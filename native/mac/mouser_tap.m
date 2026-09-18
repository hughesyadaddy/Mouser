/*
 * libmouser_tap.dylib -- native CGEventTap callback for Mouser (macOS).
 *
 * The session event tap sits in the delivery path of every mouse event on
 * the machine, and macOS disables a tap whose callback stalls. Mouser's
 * callback used to be Python on the Qt main thread's run loop: every pointer
 * report took the GIL, so a busy Python thread stalled all mouse input and
 * the tap was repeatedly disabled by timeout (kCGEventTapDisabledByTimeout).
 *
 * Everything here runs without entering Python, mirroring native/win:
 *
 *   - the tap lives on a dedicated thread with its own CFRunLoop;
 *   - the callback decides from one packed word (flags, interest mask, block
 *     mask) Python pushes whenever its state changes -- no callback into
 *     Python, no allocation, no lock;
 *   - only events Mouser acts on go into a lock-free SPSC ring that a Python
 *     drain thread collects with the GIL released;
 *   - Logitech wheel attribution (the basis of the OS-layer scroll invert)
 *     comes from an IOHIDManager scheduled on the same run loop, so it never
 *     waits on Python either.
 *
 * The decision table is `tap_decide`, a pure function of the packed filter
 * and the event's fields. It is exported for tests and must stay in step
 * with core/native_hook_mac.py (`decide`), which is the Python fallback's
 * contract as well.
 *
 * Build: python3 native/mac/build.py
 */

#import <Foundation/Foundation.h>
#include <CoreFoundation/CoreFoundation.h>
#include <CoreGraphics/CoreGraphics.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <dispatch/dispatch.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdatomic.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MOUSER_TAP_ABI 1u

#define TAP_STOP_WAIT_MS 2000u
#define TAP_START_WAIT_MS 5000u

#define EXPORT __attribute__((visibility("default")))

/* -- filter flags: mirror core/native_hook_filter.py + core/native_hook_mac.py */

#define FILTER_INTERCEPT        (1u << 0)
#define FILTER_VSCROLL_INVERT   (1u << 1)
#define FILTER_HSCROLL_INVERT   (1u << 2)
#define FILTER_DEBUG            (1u << 3)
#define FILTER_CAPTURE          (1u << 4)
#define FILTER_IGNORE_TRACKPAD  (1u << 5)
#define FILTER_THUMB_VIA_HID    (1u << 6)
#define FILTER_SENSE_PANEL      (1u << 7)

/* -- event codes: mirror core/native_hook_filter.py + core/native_hook_mac.py */

#define EVT_NONE              0u
#define EVT_XBUTTON1_DOWN     1u
#define EVT_XBUTTON1_UP       2u
#define EVT_XBUTTON2_DOWN     3u
#define EVT_XBUTTON2_UP       4u
#define EVT_MIDDLE_DOWN       5u
#define EVT_MIDDLE_UP         6u
#define EVT_HSCROLL_LEFT      7u
#define EVT_HSCROLL_RIGHT     8u
#define EVT_THUMB_DOWN        9u
#define EVT_THUMB_UP          10u
#define EVT_SENSE_PANEL_DOWN  11u
#define EVT_SENSE_PANEL_UP    12u

#define EVENT_BIT(code) (1u << (code))

#define PAIRABLE_DOWN_BITS \
    (EVENT_BIT(EVT_XBUTTON1_DOWN) | EVENT_BIT(EVT_XBUTTON2_DOWN) | \
     EVENT_BIT(EVT_MIDDLE_DOWN) | EVENT_BIT(EVT_THUMB_DOWN))

/* kCGEventSourceUserData values on events Mouser must pass through: its own
 * injections (key_simulator) and Deskflow's relayed pointer/wheel. */
#define MARKER_MOUSER   0x4D4F5554ll
#define MARKER_DESKFLOW 0x44534B46ll

#define BTN_MIDDLE   2
#define BTN_BACK     3
#define BTN_FORWARD  4
#define BTN_OS_EXTRA 6

/* Mirrors LOGITECH_SCROLL_RECENT_S (0.080s). */
#define LOGITECH_WHEEL_RECENT_MS 80ull

/* Mirrors macos_iokit_scroll.PERMISSION_RETRY_S. */
#define HID_MONITOR_RETRY_S 60.0

#define LOGI_VENDOR_ID 0x046D
#define HID_PAGE_GENERIC_DESKTOP 0x01
#define HID_USAGE_MOUSE 0x02
#define HID_USAGE_WHEEL 0x38
#define HID_PAGE_CONSUMER 0x0C
#define HID_USAGE_AC_PAN 0x0238

#define CG_SCROLL_PHASE_NONE  0
#define CG_SCROLL_PHASE_ENDED 4

#define RING_SIZE 256u
#define RING_MASK (RING_SIZE - 1u)

/* Decision outcomes. */
#define ACT_PASS   0u
#define ACT_DROP   1u
#define ACT_INVERT_V (1u << 1)
#define ACT_INVERT_H (1u << 2)
#define ACT_QUEUE    (1u << 3)

typedef struct {
    int64_t  user_data;
    uint32_t event_type;
    uint32_t event_code;
    int32_t  button;
    int32_t  h_fixed;
    int32_t  v_fixed;
    uint32_t blocked;
} MouserTapEvent;

typedef char mouser_tap_event_layout_check[
    (sizeof(MouserTapEvent) == 32) ? 1 : -1];

/* Everything `tap_decide` may consult, read from the CGEvent by the callback
 * only as far as the event type requires. */
typedef struct {
    uint32_t event_type;
    int64_t  user_data;
    int32_t  button;
    int32_t  is_continuous;
    int32_t  momentum_phase;
    int32_t  scroll_phase;
    int32_t  h_fixed;
    int32_t  v_fixed;
    int32_t  recent_logitech_wheel;
} TapFields;

typedef struct {
    uint32_t action;
    uint32_t event_code;
    uint32_t blocked;
} TapDecision;

/* -- state ------------------------------------------------------------- */

#define FILTER_FLAGS_SHIFT    0
#define FILTER_INTEREST_SHIFT 16
#define FILTER_BLOCK_SHIFT    32
#define FILTER_FIELD_MASK     0xFFFFull

static _Atomic uint64_t g_filter;
static _Atomic int g_enabled = 1;
static _Atomic int g_stop;
static _Atomic uint32_t g_reenabled;
static _Atomic uint32_t g_dropped;
static _Atomic int g_hid_monitor_open;

static pthread_t g_thread;
static int g_thread_started;
static dispatch_semaphore_t g_ready;
static dispatch_semaphore_t g_done;
static dispatch_semaphore_t g_sem;
static int g_start_ok;

static CFMachPortRef g_tap;
static CFRunLoopSourceRef g_source;
/* g_loop is published to other threads for CFRunLoopPerformBlock / Stop;
 * the lock covers its lifetime, never the callback path. */
static CFRunLoopRef g_loop;
static pthread_mutex_t g_loop_lock = PTHREAD_MUTEX_INITIALIZER;
static IOHIDManagerRef g_hid_manager;
static CFRunLoopTimerRef g_hid_retry_timer;

/* Tap thread only. */
static unsigned int g_blocked_down_active;

static _Atomic uint64_t g_last_logitech_wheel_ms;

static _Atomic int32_t g_capture_dx;
static _Atomic int32_t g_capture_dy;
static CGPoint g_capture_anchor;
static _Atomic int g_capture_have_anchor;

static MouserTapEvent g_ring[RING_SIZE];
static _Atomic uint32_t g_ring_head;
static _Atomic uint32_t g_ring_tail;

/* -- helpers ------------------------------------------------------------ */

static uint64_t now_ms(void)
{
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1000000ull;
}

static void ring_push(const MouserTapEvent *event)
{
    uint32_t head = atomic_load_explicit(&g_ring_head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&g_ring_tail, memory_order_acquire);

    if (head - tail >= RING_SIZE) {
        atomic_fetch_add(&g_dropped, 1u);
        return;
    }
    g_ring[head & RING_MASK] = *event;
    atomic_store_explicit(&g_ring_head, head + 1u, memory_order_release);
    dispatch_semaphore_signal(g_sem);
}

static bool wheel_is_logitech(void)
{
    uint64_t marked = atomic_load(&g_last_logitech_wheel_ms);
    return marked != 0 && now_ms() - marked <= LOGITECH_WHEEL_RECENT_MS;
}

static void warp_to_anchor(void)
{
    if (!atomic_load(&g_capture_have_anchor)) {
        return;
    }
    CGWarpMouseCursorPosition(g_capture_anchor);
    /* Defeats the ~250ms post-warp delta suppression macOS applies. */
    CGAssociateMouseAndMouseCursorPosition(true);
}

/* Mirrors BaseMouseHook._pair_blocked_updown with the thumb button added. */
static bool pair_blocked_updown(unsigned int event_code, bool should_block)
{
    unsigned int down_code;

    switch (event_code) {
    case EVT_XBUTTON1_UP: down_code = EVT_XBUTTON1_DOWN; break;
    case EVT_XBUTTON2_UP: down_code = EVT_XBUTTON2_DOWN; break;
    case EVT_MIDDLE_UP:   down_code = EVT_MIDDLE_DOWN;   break;
    case EVT_THUMB_UP:    down_code = EVT_THUMB_DOWN;    break;
    default:
        if (should_block && (EVENT_BIT(event_code) & PAIRABLE_DOWN_BITS)) {
            g_blocked_down_active |= EVENT_BIT(event_code);
        }
        return should_block;
    }
    {
        bool down_was_blocked =
            (g_blocked_down_active & EVENT_BIT(down_code)) != 0;
        g_blocked_down_active &= ~EVENT_BIT(down_code);
        return should_block && down_was_blocked;
    }
}

/* -- the decision table -------------------------------------------------- */

/* Mirrors MouseHook._scroll_event_targets_logitech. */
static bool scroll_targets_logitech(const TapFields *f, unsigned int flags)
{
    if ((flags & FILTER_IGNORE_TRACKPAD) && f->is_continuous) {
        return false;
    }
    if (f->momentum_phase) {
        return false;
    }
    if (f->scroll_phase != CG_SCROLL_PHASE_NONE &&
        f->scroll_phase != CG_SCROLL_PHASE_ENDED) {
        return false;
    }
    return f->recent_logitech_wheel != 0;
}

static uint32_t invert_actions(const TapFields *f, unsigned int flags)
{
    uint32_t action = 0;
    if (!(flags & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT))) {
        return 0;
    }
    if (!scroll_targets_logitech(f, flags)) {
        return 0;
    }
    if (flags & FILTER_VSCROLL_INVERT) {
        action |= ACT_INVERT_V;
    }
    if (flags & FILTER_HSCROLL_INVERT) {
        action |= ACT_INVERT_H;
    }
    return action;
}

/* Pure: the same early-return set as MouseHook._event_tap_callback, in the
 * same order. `pairing` is the only state it touches (tap thread only). */
static void tap_decide(uint64_t filter, const TapFields *f, bool pairing,
                       TapDecision *out)
{
    unsigned int flags = (unsigned int)((filter >> FILTER_FLAGS_SHIFT) & FILTER_FIELD_MASK);
    unsigned int interest = (unsigned int)((filter >> FILTER_INTEREST_SHIFT) & FILTER_FIELD_MASK);
    unsigned int block = (unsigned int)((filter >> FILTER_BLOCK_SHIFT) & FILTER_FIELD_MASK);
    bool debug = (flags & FILTER_DEBUG) != 0;
    uint32_t type = f->event_type;

    out->action = ACT_PASS;
    out->event_code = EVT_NONE;
    out->blocked = 0;

    if (type == kCGEventMouseMoved || type == kCGEventOtherMouseDragged) {
        if (flags & FILTER_CAPTURE) {
            out->action = ACT_DROP;
        }
        return;
    }

    if (f->user_data == MARKER_MOUSER || f->user_data == MARKER_DESKFLOW) {
        return;
    }

    if (!(flags & FILTER_INTERCEPT)) {
        if (type == kCGEventScrollWheel) {
            out->action = invert_actions(f, flags);
        }
        return;
    }

    if (type == kCGEventScrollWheel) {
        if ((flags & FILTER_IGNORE_TRACKPAD) && f->is_continuous) {
            return;
        }
        if (f->h_fixed != 0) {
            out->event_code = f->h_fixed > 0 ? EVT_HSCROLL_RIGHT : EVT_HSCROLL_LEFT;
            out->blocked = (block & EVENT_BIT(out->event_code)) != 0;
            if ((interest & EVENT_BIT(out->event_code)) || debug) {
                out->action |= ACT_QUEUE;
            }
            if (out->blocked) {
                out->action |= ACT_DROP;
                return;
            }
        } else if (debug) {
            out->action |= ACT_QUEUE;
        }
        out->action |= invert_actions(f, flags);
        return;
    }

    if (type != kCGEventOtherMouseDown && type != kCGEventOtherMouseUp) {
        return;
    }

    {
        bool down = type == kCGEventOtherMouseDown;
        if (debug) {
            out->action |= ACT_QUEUE;
        }
        switch (f->button) {
        case BTN_MIDDLE:
            out->event_code = down ? EVT_MIDDLE_DOWN : EVT_MIDDLE_UP;
            break;
        case BTN_BACK:
            out->event_code = down ? EVT_XBUTTON1_DOWN : EVT_XBUTTON1_UP;
            break;
        case BTN_FORWARD:
            out->event_code = down ? EVT_XBUTTON2_DOWN : EVT_XBUTTON2_UP;
            break;
        case BTN_OS_EXTRA:
            if (flags & FILTER_SENSE_PANEL) {
                out->event_code = down ? EVT_SENSE_PANEL_DOWN : EVT_SENSE_PANEL_UP;
                out->blocked = 1;
                out->action |= ACT_QUEUE | ACT_DROP;
                return;
            }
            if (flags & FILTER_THUMB_VIA_HID) {
                out->blocked = 1;
                out->action |= ACT_DROP;
                return;
            }
            out->event_code = down ? EVT_THUMB_DOWN : EVT_THUMB_UP;
            break;
        default:
            return;
        }
    }

    {
        bool should_block = (block & EVENT_BIT(out->event_code)) != 0;
        if (pairing) {
            should_block = pair_blocked_updown(out->event_code, should_block);
        }
        out->blocked = should_block ? 1u : 0u;
        if ((interest & EVENT_BIT(out->event_code)) || debug) {
            out->action |= ACT_QUEUE;
        }
        if (should_block) {
            out->action |= ACT_DROP;
        }
    }
}

/* -- the tap callback ---------------------------------------------------- */

static void negate_axis(CGEventRef event, int axis)
{
    CGEventField fields[3];
    if (axis == 1) {
        fields[0] = kCGScrollWheelEventDeltaAxis1;
        fields[1] = kCGScrollWheelEventFixedPtDeltaAxis1;
        fields[2] = kCGScrollWheelEventPointDeltaAxis1;
    } else {
        fields[0] = kCGScrollWheelEventDeltaAxis2;
        fields[1] = kCGScrollWheelEventFixedPtDeltaAxis2;
        fields[2] = kCGScrollWheelEventPointDeltaAxis2;
    }
    for (int i = 0; i < 3; i++) {
        int64_t value = CGEventGetIntegerValueField(event, fields[i]);
        if (value) {
            CGEventSetIntegerValueField(event, fields[i], -value);
        }
    }
}

static CGEventRef tap_callback(CGEventTapProxy proxy, CGEventType type,
                               CGEventRef event, void *refcon)
{
    TapFields f;
    TapDecision d;
    uint64_t filter;
    unsigned int flags;
    (void)proxy;
    (void)refcon;

    if (type == kCGEventTapDisabledByTimeout ||
        type == kCGEventTapDisabledByUserInput) {
        /* A programmatic CGEventTapEnable(false) delivers this too, so only
         * a tap Python wants enabled is put back -- and only that counts. */
        if (atomic_load(&g_enabled) && g_tap != NULL) {
            CGEventTapEnable(g_tap, true);
            atomic_fetch_add(&g_reenabled, 1u);
        }
        return event;
    }

    filter = atomic_load(&g_filter);
    flags = (unsigned int)((filter >> FILTER_FLAGS_SHIFT) & FILTER_FIELD_MASK);

    /* Pointer motion: the 1 kHz path. Nothing but a live directional
     * capture ever needs it, so decide from the flag word alone. */
    if (type == kCGEventMouseMoved || type == kCGEventOtherMouseDragged) {
        if (!(flags & FILTER_CAPTURE)) {
            return event;
        }
        atomic_fetch_add(&g_capture_dx,
                         (int32_t)CGEventGetIntegerValueField(event, kCGMouseEventDeltaX));
        atomic_fetch_add(&g_capture_dy,
                         (int32_t)CGEventGetIntegerValueField(event, kCGMouseEventDeltaY));
        warp_to_anchor();
        return NULL;
    }

    memset(&f, 0, sizeof(f));
    f.event_type = (uint32_t)type;
    f.user_data = CGEventGetIntegerValueField(event, kCGEventSourceUserData);
    if (type == kCGEventScrollWheel) {
        f.is_continuous = (int32_t)CGEventGetIntegerValueField(
            event, kCGScrollWheelEventIsContinuous);
        f.momentum_phase = (int32_t)CGEventGetIntegerValueField(
            event, kCGScrollWheelEventMomentumPhase);
        f.scroll_phase = (int32_t)CGEventGetIntegerValueField(
            event, kCGScrollWheelEventScrollPhase);
        f.h_fixed = (int32_t)CGEventGetIntegerValueField(
            event, kCGScrollWheelEventFixedPtDeltaAxis2);
        f.v_fixed = (int32_t)CGEventGetIntegerValueField(
            event, kCGScrollWheelEventFixedPtDeltaAxis1);
        f.recent_logitech_wheel = wheel_is_logitech() ? 1 : 0;
    } else if (type == kCGEventOtherMouseDown || type == kCGEventOtherMouseUp) {
        f.button = (int32_t)CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber);
    }

    tap_decide(filter, &f, true, &d);

    if (d.action & ACT_QUEUE) {
        MouserTapEvent queued;
        queued.user_data = f.user_data;
        queued.event_type = f.event_type;
        queued.event_code = d.event_code;
        queued.button = f.button;
        queued.h_fixed = f.h_fixed;
        queued.v_fixed = f.v_fixed;
        queued.blocked = d.blocked;
        ring_push(&queued);
    }
    if (d.action & ACT_DROP) {
        return NULL;
    }
    if (d.action & ACT_INVERT_V) {
        negate_axis(event, 1);
    }
    if (d.action & ACT_INVERT_H) {
        negate_axis(event, 2);
    }
    return event;
}

/* -- Logitech wheel monitor (IOHID, same run loop) ------------------------ */

static CFDictionaryRef cf_dict_num(const char *k1, int v1, const char *k2, int v2,
                                   const char *k3, int v3)
{
    CFStringRef keys[3];
    CFNumberRef values[3];
    CFIndex count = k3 != NULL ? 3 : 2;
    keys[0] = CFStringCreateWithCString(NULL, k1, kCFStringEncodingUTF8);
    keys[1] = CFStringCreateWithCString(NULL, k2, kCFStringEncodingUTF8);
    keys[2] = k3 != NULL ? CFStringCreateWithCString(NULL, k3, kCFStringEncodingUTF8) : NULL;
    values[0] = CFNumberCreate(NULL, kCFNumberIntType, &v1);
    values[1] = CFNumberCreate(NULL, kCFNumberIntType, &v2);
    values[2] = k3 != NULL ? CFNumberCreate(NULL, kCFNumberIntType, &v3) : NULL;
    CFDictionaryRef dict = CFDictionaryCreate(
        NULL, (const void **)keys, (const void **)values, count,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    for (CFIndex i = 0; i < count; i++) {
        CFRelease(keys[i]);
        CFRelease(values[i]);
    }
    return dict;
}

static void hid_value_callback(void *context, IOReturn result, void *sender,
                               IOHIDValueRef value)
{
    (void)context;
    (void)result;
    (void)sender;
    if (value == NULL) {
        return;
    }
    IOHIDElementRef element = IOHIDValueGetElement(value);
    if (element == NULL) {
        return;
    }
    uint32_t page = IOHIDElementGetUsagePage(element);
    uint32_t usage = IOHIDElementGetUsage(element);
    if ((page == HID_PAGE_GENERIC_DESKTOP && usage == HID_USAGE_WHEEL) ||
        (page == HID_PAGE_CONSUMER && usage == HID_USAGE_AC_PAN)) {
        atomic_store(&g_last_logitech_wheel_ms, now_ms());
    }
}

static void hid_monitor_stop(void)
{
    if (g_hid_retry_timer != NULL) {
        CFRunLoopTimerInvalidate(g_hid_retry_timer);
        CFRelease(g_hid_retry_timer);
        g_hid_retry_timer = NULL;
    }
    if (g_hid_manager != NULL) {
        IOHIDManagerUnscheduleFromRunLoop(g_hid_manager, g_loop, kCFRunLoopDefaultMode);
        if (atomic_load(&g_hid_monitor_open)) {
            IOHIDManagerClose(g_hid_manager, kIOHIDOptionsTypeNone);
        }
        CFRelease(g_hid_manager);
        g_hid_manager = NULL;
    }
    atomic_store(&g_hid_monitor_open, 0);
}

static void hid_monitor_try_open(void);

static void hid_retry_fired(CFRunLoopTimerRef timer, void *info)
{
    (void)timer;
    (void)info;
    hid_monitor_try_open();
}

/* Tap thread only. Opening needs Input Monitoring; a denial is retried on a
 * timer rather than per event, and until it succeeds no wheel is attributed
 * to the Logitech (invert fails closed, as the Python monitor does). */
static void hid_monitor_try_open(void)
{
    if (atomic_load(&g_hid_monitor_open)) {
        return;
    }
    if (g_hid_manager == NULL) {
        CFDictionaryRef matching;
        CFDictionaryRef elements[2];
        CFArrayRef element_matching;

        g_hid_manager = IOHIDManagerCreate(NULL, kIOHIDOptionsTypeNone);
        if (g_hid_manager == NULL) {
            return;
        }
        matching = cf_dict_num(kIOHIDVendorIDKey, LOGI_VENDOR_ID,
                               kIOHIDPrimaryUsagePageKey, HID_PAGE_GENERIC_DESKTOP,
                               kIOHIDPrimaryUsageKey, HID_USAGE_MOUSE);
        IOHIDManagerSetDeviceMatching(g_hid_manager, matching);
        CFRelease(matching);
        elements[0] = cf_dict_num(kIOHIDElementUsagePageKey, HID_PAGE_GENERIC_DESKTOP,
                                  kIOHIDElementUsageKey, HID_USAGE_WHEEL, NULL, 0);
        elements[1] = cf_dict_num(kIOHIDElementUsagePageKey, HID_PAGE_CONSUMER,
                                  kIOHIDElementUsageKey, HID_USAGE_AC_PAN, NULL, 0);
        element_matching = CFArrayCreate(NULL, (const void **)elements, 2,
                                         &kCFTypeArrayCallBacks);
        CFRelease(elements[0]);
        CFRelease(elements[1]);
        IOHIDManagerSetInputValueMatchingMultiple(g_hid_manager, element_matching);
        CFRelease(element_matching);
        IOHIDManagerRegisterInputValueCallback(g_hid_manager, hid_value_callback, NULL);
        IOHIDManagerScheduleWithRunLoop(g_hid_manager, g_loop, kCFRunLoopDefaultMode);
    }
    if (IOHIDManagerOpen(g_hid_manager, kIOHIDOptionsTypeNone) == kIOReturnSuccess) {
        atomic_store(&g_hid_monitor_open, 1);
        if (g_hid_retry_timer != NULL) {
            CFRunLoopTimerInvalidate(g_hid_retry_timer);
            CFRelease(g_hid_retry_timer);
            g_hid_retry_timer = NULL;
        }
        return;
    }
    if (g_hid_retry_timer == NULL) {
        g_hid_retry_timer = CFRunLoopTimerCreate(
            NULL, CFAbsoluteTimeGetCurrent() + HID_MONITOR_RETRY_S,
            HID_MONITOR_RETRY_S, 0, 0, hid_retry_fired, NULL);
        CFRunLoopAddTimer(g_loop, g_hid_retry_timer, kCFRunLoopDefaultMode);
    }
}

/* -- tap thread ------------------------------------------------------------ */

static void tap_teardown(void)
{
    hid_monitor_stop();
    if (g_tap != NULL) {
        CGEventTapEnable(g_tap, false);
    }
    if (g_source != NULL) {
        CFRunLoopRemoveSource(g_loop, g_source, kCFRunLoopCommonModes);
        CFRelease(g_source);
        g_source = NULL;
    }
    if (g_tap != NULL) {
        CFMachPortInvalidate(g_tap);
        CFRelease(g_tap);
        g_tap = NULL;
    }
    pthread_mutex_lock(&g_loop_lock);
    if (g_loop != NULL) {
        CFRelease(g_loop);
        g_loop = NULL;
    }
    pthread_mutex_unlock(&g_loop_lock);
}

/* Queue `block` on the tap thread; false when there is no loop to run it. */
static bool perform_on_tap_loop(dispatch_block_t block)
{
    bool queued = false;
    pthread_mutex_lock(&g_loop_lock);
    if (g_loop != NULL) {
        CFRunLoopPerformBlock(g_loop, kCFRunLoopCommonModes, block);
        CFRunLoopWakeUp(g_loop);
        queued = true;
    }
    pthread_mutex_unlock(&g_loop_lock);
    return queued;
}

static void *tap_thread_main(void *arg)
{
    CGEventMask mask;
    (void)arg;

    pthread_setname_np("MouserTap");
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);

    mask = CGEventMaskBit(kCGEventMouseMoved)
         | CGEventMaskBit(kCGEventOtherMouseDown)
         | CGEventMaskBit(kCGEventOtherMouseUp)
         | CGEventMaskBit(kCGEventOtherMouseDragged)
         | CGEventMaskBit(kCGEventScrollWheel);

    g_tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                             kCGEventTapOptionDefault, mask, tap_callback, NULL);
    if (g_tap == NULL) {
        g_start_ok = 0;
        dispatch_semaphore_signal(g_ready);
        dispatch_semaphore_signal(g_done);
        return NULL;
    }
    pthread_mutex_lock(&g_loop_lock);
    g_loop = CFRunLoopGetCurrent();
    CFRetain(g_loop);
    pthread_mutex_unlock(&g_loop_lock);
    g_source = CFMachPortCreateRunLoopSource(NULL, g_tap, 0);
    CFRunLoopAddSource(g_loop, g_source, kCFRunLoopCommonModes);
    CGEventTapEnable(g_tap, atomic_load(&g_enabled) != 0);
    g_start_ok = 1;
    dispatch_semaphore_signal(g_ready);

    while (!atomic_load(&g_stop)) {
        SInt32 rc;
        @autoreleasepool {
            rc = CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, false);
        }
        if (rc == kCFRunLoopRunFinished) {
            /* No sources left (port invalidated): do not spin. */
            usleep(250000);
        }
    }

    tap_teardown();
    dispatch_semaphore_signal(g_done);
    return NULL;
}

/* -- exports --------------------------------------------------------------- */

EXPORT unsigned int mouser_tap_abi_version(void)
{
    return MOUSER_TAP_ABI;
}

EXPORT unsigned int mouser_tap_event_size(void)
{
    return (unsigned int)sizeof(MouserTapEvent);
}

EXPORT int mouser_tap_stop(void);

EXPORT int mouser_tap_start(void)
{
    if (g_thread_started) {
        /* A stop that timed out left a thread on its way down; nothing
         * can be started against it. */
        return atomic_load(&g_stop) ? 0 : g_start_ok;
    }
    if (g_sem == NULL) {
        g_sem = dispatch_semaphore_create(0);
    }
    atomic_store(&g_ring_head, 0u);
    atomic_store(&g_ring_tail, 0u);
    g_ready = dispatch_semaphore_create(0);
    g_done = dispatch_semaphore_create(0);
    atomic_store(&g_stop, 0);
    g_blocked_down_active = 0u;
    g_start_ok = 0;
    if (pthread_create(&g_thread, NULL, tap_thread_main, NULL) != 0) {
        return 0;
    }
    g_thread_started = 1;
    dispatch_semaphore_wait(
        g_ready, dispatch_time(DISPATCH_TIME_NOW, (int64_t)TAP_START_WAIT_MS * NSEC_PER_MSEC));
    if (!g_start_ok) {
        mouser_tap_stop();
        return 0;
    }
    return 1;
}

EXPORT int mouser_tap_stop(void)
{
    if (!g_thread_started) {
        return 1;
    }
    atomic_store(&g_stop, 1);
    pthread_mutex_lock(&g_loop_lock);
    if (g_loop != NULL) {
        CFRunLoopStop(g_loop);
    }
    pthread_mutex_unlock(&g_loop_lock);
    if (dispatch_semaphore_wait(
            g_done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)TAP_STOP_WAIT_MS * NSEC_PER_MSEC)) != 0) {
        /* Wedged: leave the thread rather than tear the tap out from under
         * a callback that may still be running. */
        return 0;
    }
    pthread_join(g_thread, NULL);
    g_thread_started = 0;
    g_blocked_down_active = 0u;
    atomic_store(&g_capture_dx, 0);
    atomic_store(&g_capture_dy, 0);
    atomic_store(&g_capture_have_anchor, 0);
    return 1;
}

/* Thread-safe; the CGEventTapEnable itself runs on the tap thread. */
EXPORT void mouser_tap_set_enabled(int enabled)
{
    atomic_store(&g_enabled, enabled ? 1 : 0);
    perform_on_tap_loop(^{
        if (g_tap != NULL) {
            CGEventTapEnable(g_tap, atomic_load(&g_enabled) != 0);
        }
    });
}

EXPORT int mouser_tap_is_enabled(void)
{
    return g_tap != NULL && CGEventTapIsEnabled(g_tap) ? 1 : 0;
}

EXPORT void mouser_tap_set_filter(unsigned int flags, unsigned int interest_mask,
                                  unsigned int block_mask)
{
    uint64_t packed =
        ((uint64_t)(flags & FILTER_FIELD_MASK) << FILTER_FLAGS_SHIFT) |
        ((uint64_t)(interest_mask & FILTER_FIELD_MASK) << FILTER_INTEREST_SHIFT) |
        ((uint64_t)(block_mask & FILTER_FIELD_MASK) << FILTER_BLOCK_SHIFT);
    uint64_t previous = atomic_exchange(&g_filter, packed);
    unsigned int was = (unsigned int)((previous >> FILTER_FLAGS_SHIFT) & FILTER_FIELD_MASK);

    /* The wheel monitor needs Input Monitoring, so it is only opened once an
     * invert fallback is armed (a physical Logitech is bound and the user
     * asked for it) -- the same moment the Python monitor would have. */
    if ((flags & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT)) &&
        !(was & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT)) &&
        !atomic_load(&g_hid_monitor_open)) {
        perform_on_tap_loop(^{
            hid_monitor_try_open();
        });
    }

    /* The capture lasts exactly as long as Python holds the flag, as the
     * Python tap's did: every path that can lose the release (focus flip,
     * device unbind, listener teardown) pushes it clear, and a Python that
     * is wedged outright is the watchdog's job. */
    if ((flags & FILTER_CAPTURE) && !(was & FILTER_CAPTURE)) {
        /* Fresh stroke: clear the accumulator and pin the pointer where it
         * is now, mirroring _arm_gesture_anchor. */
        CGEventRef probe = CGEventCreate(NULL);
        atomic_store(&g_capture_dx, 0);
        atomic_store(&g_capture_dy, 0);
        if (probe != NULL) {
            g_capture_anchor = CGEventGetLocation(probe);
            CFRelease(probe);
            atomic_store(&g_capture_have_anchor, 1);
            warp_to_anchor();
        }
    } else if (!(flags & FILTER_CAPTURE) && (was & FILTER_CAPTURE)) {
        /* Mirrors _release_gesture_anchor. */
        atomic_store(&g_capture_have_anchor, 0);
        CGWarpMouseCursorPosition(g_capture_anchor);
        CGAssociateMouseAndMouseCursorPosition(true);
    }
}

EXPORT void mouser_tap_take_capture_delta(int *dx, int *dy)
{
    int32_t x = atomic_exchange(&g_capture_dx, 0);
    int32_t y = atomic_exchange(&g_capture_dy, 0);
    if (dx != NULL) {
        *dx = (int)x;
    }
    if (dy != NULL) {
        *dy = (int)y;
    }
}

EXPORT int mouser_tap_next_event(MouserTapEvent *out, unsigned int timeout_ms)
{
    uint32_t tail;

    if (out == NULL || g_sem == NULL) {
        return 0;
    }
    if (dispatch_semaphore_wait(
            g_sem, dispatch_time(DISPATCH_TIME_NOW, (int64_t)timeout_ms * NSEC_PER_MSEC)) != 0) {
        return 0;
    }
    tail = atomic_load_explicit(&g_ring_tail, memory_order_relaxed);
    if (tail == atomic_load_explicit(&g_ring_head, memory_order_acquire)) {
        return 0;
    }
    *out = g_ring[tail & RING_MASK];
    atomic_store_explicit(&g_ring_tail, tail + 1u, memory_order_release);
    return 1;
}

EXPORT unsigned int mouser_tap_dropped(void)
{
    return atomic_load(&g_dropped);
}

EXPORT unsigned int mouser_tap_reenabled(void)
{
    return atomic_load(&g_reenabled);
}

EXPORT int mouser_tap_hid_monitor_open(void)
{
    return atomic_load(&g_hid_monitor_open);
}

/* Test entry: the decision table on caller-supplied fields, with the
 * down/up pairing state left untouched. */
EXPORT void mouser_tap_decide(unsigned int flags, unsigned int interest_mask,
                              unsigned int block_mask, const TapFields *fields,
                              TapDecision *out)
{
    uint64_t packed =
        ((uint64_t)(flags & FILTER_FIELD_MASK) << FILTER_FLAGS_SHIFT) |
        ((uint64_t)(interest_mask & FILTER_FIELD_MASK) << FILTER_INTEREST_SHIFT) |
        ((uint64_t)(block_mask & FILTER_FIELD_MASK) << FILTER_BLOCK_SHIFT);
    if (fields == NULL || out == NULL) {
        return;
    }
    tap_decide(packed, fields, false, out);
}

EXPORT unsigned int mouser_tap_fields_size(void)
{
    return (unsigned int)sizeof(TapFields);
}

EXPORT unsigned int mouser_tap_decision_size(void)
{
    return (unsigned int)sizeof(TapDecision);
}
