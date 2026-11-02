// W2 presentation only. Capture state and actions belong to MeetingRecording.
import QtQuick 2.15
import QtQuick.Controls 2.15
import "."
import "components"

FocusScope {
    id: root
    objectName: "meetingOverlay"
    property var presentation: ({})
    property var texts: ({})
    property bool compact: false
    property bool keyboardMode: false
    property bool freezeAnimations: false
    signal pauseClicked()
    signal resumeClicked()
    signal stopClicked()
    signal participantsClicked()
    signal extendClicked()
    signal resumeMicrophoneClicked()
    signal keyboardRequested()
    signal releaseKeyboardRequested()
    signal dragRequested(real dx, real dy)
    readonly property string avState: presentation.state || "hidden"
    readonly property bool paused: avState === "paused" || avState === "paused-lock" || avState === "paused-indicator" || avState === "paused-hang"
    readonly property bool terminal: avState === "stopping" || avState === "error"
    readonly property bool effectiveCompact: compact && !terminal
    readonly property bool hasSecondary: !effectiveCompact && avState !== "error" && !!presentation.secondary
    readonly property real pillWidth: effectiveCompact ? PillTheme.pillMeetingBarCompactW : Math.max(terminal ? 0 : PillTheme.pillMeetingBarMinW, Math.min(PillTheme.pillMeetingBarMaxW, row1.width + PillTheme.pillMeetingBarPaddingLeft + PillTheme.pillMeetingBarPaddingRight))
    readonly property real pillHeight: hasSecondary ? Math.floor(Math.max(PillTheme.pillMeetingBarMultilineHMin, secondary.y + secondary.height + PillTheme.pillMeetingBarMultilinePaddingBottom)) : PillTheme.pillH
    width: pillWidth
    height: pillHeight
    implicitWidth: pillWidth
    implicitHeight: pillHeight
    visible: !!presentation.visible
    Accessible.role: Accessible.AlertMessage
    Accessible.name: (terminal ? title.text : (paused ? (texts.paused || "") : (texts.recording || ""))) + (hasSecondary ? ". " + presentation.secondary : "")
    Keys.onEscapePressed: {
        compact = true;
        releaseKeyboardRequested();
        event.accepted = true;
    }
    function forceLayout() { row1.forceLayout(); secondaryText.forceLayout(); }
    function focusFirstControl() {
        if (compact) collapse.forceActiveFocus();
        else if (!terminal) pause.forceActiveFocus();
    }
    OverlaySurface {
        surfaceWidth: root.pillWidth
        surfaceHeight: root.pillHeight
        surfaceRadius: root.hasSecondary ? PillTheme.pillMeetingBarMultilineRadius : PillTheme.pillRadius
    }
    Row {
        id: row1
        x: PillTheme.pillMeetingBarPaddingLeft
        height: PillTheme.pillH
        spacing: PillTheme.pillMeetingBarGap
        Item {
            id: grip
            objectName: "meetingGrip"
            width: PillTheme.pillMeetingBarGripW
            height: PillTheme.pillMeetingBarGripH
            y: (row1.height - height) / 2
            Repeater {
                model: PillTheme.pillMeetingBarGripCols * PillTheme.pillMeetingBarGripRows
                Rectangle {
                    required property int index
                    x: (index % PillTheme.pillMeetingBarGripCols) * 4
                    y: Math.floor(index / PillTheme.pillMeetingBarGripCols) * 5 + 2
                    width: PillTheme.pillMeetingBarGripDot
                    height: width
                    radius: width / 2
                    color: PillTheme.pillIconMuted
                    opacity: PillTheme.pillMeetingBarGripOpacity
                }
            }
            MouseArea {
                anchors.fill: parent
                property real previousX: 0
                property real previousY: 0
                onPressed: { var p = mapToGlobal(mouse.x, mouse.y); previousX = p.x; previousY = p.y; }
                onPositionChanged: if (pressed) {
                    var p = mapToGlobal(mouse.x, mouse.y);
                    root.dragRequested(p.x - previousX, p.y - previousY);
                    previousX = p.x; previousY = p.y;
                }
            }
            Accessible.name: root.texts.grip || ""
        }
        Item {
            width: PillTheme.pillMeetingBarSymbol
            height: width
            y: (row1.height - height) / 2
            Rectangle {
                anchors.centerIn: parent
                width: parent.width - 2
                height: width
                radius: width / 2
                color: "transparent"
                border.width: 1
                border.color: PillTheme.pillBarLive
                visible: !root.paused && !root.terminal && root.avState !== "mic-paused" && root.avState !== "limit-soon"
                Rectangle { anchors.centerIn: parent; width: 5; height: 5; radius: 3; color: PillTheme.pillBarLive }
            }
            Icon {
                anchors.fill: parent
                name: root.avState === "error" || root.avState === "mic-paused" ? "alert" : root.avState === "limit-soon" ? "clock" : "pause"
                visible: root.paused || root.avState === "error" || root.avState === "mic-paused" || root.avState === "limit-soon"
                // design/meetings/tokens.md §8.2: pause has its own 2.2 stroke.
                strokeWidth: name === "pause" ? 2.2 : 1.5
                color: root.avState === "error" ? PillTheme.pillError : root.avState === "mic-paused" || root.avState === "limit-soon" ? PillTheme.pillWarning : PillTheme.pillIconMuted
            }
            Canvas {
                id: stoppingSpinner
                objectName: "meetingStoppingSpinner"
                anchors.fill: parent
                visible: root.avState === "stopping"
                antialiasing: true
                property color strokeColor: PillTheme.pillIconMuted
                onStrokeColorChanged: requestPaint()
                onPaint: {
                    var ctx = getContext("2d");
                    ctx.reset();
                    ctx.scale(width / 16, height / 16);
                    ctx.strokeStyle = strokeColor;
                    ctx.lineWidth = 1.5;
                    ctx.lineCap = "round";
                    ctx.beginPath();
                    // 04-recording.html: M8 2a6 6 0 1 1-6 6 (no arrowhead).
                    ctx.arc(8, 8, 6, -Math.PI / 2, Math.PI);
                    ctx.stroke();
                }
                NumberAnimation on rotation {
                    objectName: "meetingStoppingRotation"
                    running: root.visible && root.avState === "stopping" && !root.freezeAnimations
                    from: 0
                    to: 360
                    duration: 1000
                    loops: Animation.Infinite
                }
            }
        }
        Item {
            visible: !root.effectiveCompact
            width: title.implicitWidth * title.scale
            height: title.implicitHeight * title.scale
            y: (row1.height - height) / 2
            Text {
                id: title
                objectName: "meetingTitle"
                textFormat: Text.PlainText
                text: root.terminal ? (root.avState === "stopping" ? (root.texts.stopping || "") : (root.presentation.secondary || root.texts.audioError || "")) : (root.paused ? (root.texts.paused || "") : (root.texts.recording || ""))
                color: root.avState === "error" ? PillTheme.pillError : PillTheme.pillFg
                font.family: "PT Root UI"
                // Qt 5 rounds pixelSize to int. Lay out at 2x to retain W2's 12.5 px.
                font.pixelSize: PillTheme.pillMeetingBarSize * 2
                font.bold: !root.terminal
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
        Text {
            textFormat: Text.PlainText
            id: timer
            objectName: "meetingTimer"
            visible: !root.terminal
            y: (row1.height - height) / 2
            text: root.presentation.elapsed || "0:00:00"
            color: root.paused ? PillTheme.pillIconNeutral : PillTheme.pillFg
            font.family: Qt.fontFamilies().indexOf("PT Mono") >= 0 ? "PT Mono" : "monospace"
            font.pixelSize: PillTheme.pillMeetingBarTimerSize
            font.letterSpacing: PillTheme.pillMeetingBarTimerTracking * PillTheme.pillMeetingBarTimerSize
            MouseArea { id: timerMouse; anchors.fill: parent; hoverEnabled: true }
            ToolTip.visible: timerMouse.containsMouse && root.paused && !!root.presentation.timerTooltip
            ToolTip.text: root.presentation.timerTooltip || ""
            ToolTip.delay: 500
        }
        Rectangle { visible: !root.effectiveCompact && !root.terminal; width: PillTheme.pillMeetingBarSepW; height: PillTheme.pillMeetingBarSepH; y: (row1.height - height) / 2; color: PillTheme.pillCloseBg }
        MeetingOverlayButton {
            id: pause
            objectName: "meetingPause"
            visible: !root.effectiveCompact && !root.terminal
            enabled: !root.paused || root.presentation.canResume === true
            y: (row1.height - height) / 2
            text: root.paused ? (root.texts.resume || "") : (root.texts.pause || "")
            iconName: root.paused ? "play" : "pause"
            keyboardMode: root.keyboardMode
            onClicked: root.paused ? root.resumeClicked() : root.pauseClicked()
        }
        MeetingOverlayButton {
            objectName: "meetingStop"
            visible: !root.effectiveCompact && !root.terminal
            y: (row1.height - height) / 2
            text: root.texts.stop || ""
            iconName: "stop"
            keyboardMode: root.keyboardMode
            onClicked: root.stopClicked()
        }
        Rectangle {
            objectName: "meetingSeparatorBeforeParticipants"
            visible: !root.effectiveCompact && !root.terminal
            width: PillTheme.pillMeetingBarSepW
            height: PillTheme.pillMeetingBarSepH
            y: (row1.height - height) / 2
            color: PillTheme.pillCloseBg
        }
        MeetingOverlayButton {
            objectName: "meetingParticipants"
            visible: !root.effectiveCompact && !root.terminal
            y: (row1.height - height) / 2
            iconOnly: true
            iconName: "chat"
            tooltip: root.texts.participants || ""
            keyboardMode: root.keyboardMode
            onClicked: root.participantsClicked()
        }
        MeetingOverlayButton {
            id: collapse
            objectName: "meetingCollapse"
            visible: !root.terminal
            y: (row1.height - height) / 2
            iconOnly: true
            iconName: root.effectiveCompact ? "expand" : "collapse"
            tooltip: root.effectiveCompact ? (root.texts.expand || "") : (root.texts.collapse || "")
            keyboardMode: root.keyboardMode
            onClicked: root.compact = !root.effectiveCompact
        }
    }
    Item {
        id: secondary
        objectName: "meetingSecondary"
        visible: root.hasSecondary
        x: PillTheme.pillMeetingBarMultilinePaddingLeft + PillTheme.pillMeetingBarMultilineIndent
        y: PillTheme.pillH + PillTheme.pillMeetingBarMultilineGap
        width: root.pillWidth - x - PillTheme.pillMeetingBarMultilinePaddingRight - PillTheme.pillMeetingBarRow2PaddingRight
        readonly property real actionWidth: extend.visible ? extend.width : resumeMicrophone.visible ? resumeMicrophone.width : 0
        readonly property real actionHeight: extend.visible ? extend.height : resumeMicrophone.visible ? resumeMicrophone.height : 0
        readonly property bool inlineAction: actionWidth > 0 && width - actionWidth - PillTheme.pillMeetingBarRow2GapX >= PillTheme.pillMeetingBarRow2TextBasis
        readonly property real inlineHeight: Math.max(secondaryLabel.height, actionHeight)
        readonly property real actionX: inlineAction ? width - actionWidth : 0
        readonly property real actionY: inlineAction ? (inlineHeight - actionHeight) / 2 : secondaryLabel.height + PillTheme.pillMeetingBarRow2GapY
        height: inlineAction ? inlineHeight : secondaryLabel.height + (actionHeight > 0 ? PillTheme.pillMeetingBarRow2GapY + actionHeight : 0)
        Item {
            id: secondaryLabel
            width: secondary.inlineAction ? secondary.width - secondary.actionWidth - PillTheme.pillMeetingBarRow2GapX : secondary.width
            height: secondaryText.implicitHeight * secondaryText.scale
            y: secondary.inlineAction ? (secondary.inlineHeight - height) / 2 : 0
            Text {
                id: secondaryText
                objectName: "meetingSecondaryText"
                textFormat: Text.PlainText
                width: parent.width / scale
                text: root.presentation.secondary || ""
                wrapMode: Text.Wrap
                color: root.avState === "app-closed" || root.avState === "notify-fallback" ? PillTheme.pillIconMuted : PillTheme.pillWarning
                font.family: "PT Root UI"
                font.pixelSize: PillTheme.pillMeetingBarSize * 2
                // 04-recording.html inherits the page's CSS line-height: 1.5.
                lineHeightMode: Text.FixedHeight
                lineHeight: PillTheme.pillMeetingBarSize * 1.5 / scale
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
        MeetingOverlayButton {
            id: extend
            objectName: "meetingExtend"
            visible: root.presentation.secondaryAction === "extend" && root.presentation.canExtend === true
            x: secondary.actionX
            y: secondary.actionY
            text: root.texts.extend || ""
            primary: true
            keyboardMode: root.keyboardMode
            onClicked: root.extendClicked()
        }
        MeetingOverlayButton {
            id: resumeMicrophone
            objectName: "meetingResumeMicrophone"
            visible: root.presentation.secondaryAction === "resume-microphone" && !!root.presentation.microphoneName
            x: secondary.actionX
            y: secondary.actionY
            text: root.presentation.microphoneName && root.presentation.microphoneName.length <= 24 ? (root.texts.resumeMicrophone || "").replace("{name}", root.presentation.microphoneName) : (root.texts.resumeOtherMicrophone || "")
            primary: true
            keyboardMode: root.keyboardMode
            onClicked: root.resumeMicrophoneClicked()
        }
    }
}
