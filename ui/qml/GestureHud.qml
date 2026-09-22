import QtQuick
import QtQuick.Window
import "Theme.js" as Theme

// Always-on gesture HUD: a frameless, click-through, top-most window that
// flashes what each swipe did -- shows even when the main window is hidden,
// so you KNOW a gesture registered (and see failures). Driven by the
// always-on backend.gestureFeedback signal (independent of debug mode).
//
// Hosted by GestureHudHost (main_qml.py) on a tiny engine of its own that is
// never torn down, unlike the settings window's engine (MainWindowHost),
// so it needs nothing from Main.qml: theme + font come from uiState.
Window {
    id: gestureHud
    readonly property var theme: Theme.palette(uiState.darkMode)
    transientParent: null
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
           | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput
    color: "transparent"
    width: 480
    height: 80
    visible: hudPill.opacity > 0
    x: Screen.virtualX + Math.round((Screen.width - width) / 2)
    y: Screen.virtualY + Math.round(Screen.height * 0.74)

    Rectangle {
        id: hudPill
        objectName: "hudPill"
        anchors.centerIn: parent
        width: hudText.implicitWidth + 44
        height: 48
        radius: 24
        opacity: 0
        color: gestureHud.theme.accent
        Behavior on opacity { NumberAnimation { duration: 160 } }

        Text {
            id: hudText
            anchors.centerIn: parent
            color: "white"
            font {
                family: uiState.fontFamily
                pixelSize: 18
                bold: true
            }
        }

        Timer {
            id: hudTimer
            interval: 900
            onTriggered: hudPill.opacity = 0
        }

        function flash(msg, status) {
            hudText.text = msg
            hudPill.color = status === "failed"
                ? "#C0392B"
                : (status === "unmapped" ? "#5A5A5A" : gestureHud.theme.accent)
            hudPill.opacity = 0.96
            hudTimer.restart()
        }
    }

    Connections {
        target: backend
        function onGestureFeedback(text, status) {
            hudPill.flash(text, status)
        }
    }
}
