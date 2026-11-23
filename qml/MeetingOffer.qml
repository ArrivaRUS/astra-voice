// W1 renders fixed copy and a visual countdown. Python admits every action.
import QtQuick 2.15
import "."
import "components"

Item {
    id: root
    objectName: "meetingOffer"
    property string kind
    property var texts: ({})
    property string remainingText
    property string errorText
    property int seconds
    property bool presented: false
    property bool starting: false
    property bool startFailed: false
    property bool freezeAnimations: false
    signal pressed(string action, bool mouse)

    readonly property real borderWidth: PillTheme.pillMeetingNoticeTextboxBorder
    readonly property real contentX: borderWidth + PillTheme.pillMeetingOfferPaddingX
    readonly property real contentY: borderWidth + PillTheme.pillMeetingOfferPaddingY
    readonly property real contentWidth: surfaceWidth - 2 * contentX
    readonly property real screenOffset: PillTheme.pillMeetingOfferScreenOffset
    readonly property real surfaceWidth: PillTheme.pillMeetingOfferW
    readonly property real surfaceHeight: startFailed ? actions.y + actions.height + contentY
                                                     : counter.y + counter.height + contentY
    implicitWidth: surfaceWidth
    implicitHeight: surfaceHeight
    visible: presented
    Accessible.role: Accessible.AlertMessage
    Accessible.name: root.startFailed ? root.errorText
                                      : ((root.starting ? root.texts.starting : root.texts.title) || "")

    function forceLayout() {
        header.forceLayout();
        title.forceLayout();
        body.forceLayout();
        remaining.forceLayout();
    }

    // Qt 5 rounds pixelSize to int. Lay out fractional sizes at 2x.
    component OfferText: Text {
        property real size: PillTheme.pillMeetingOfferBodySize
        textFormat: Text.PlainText
        font.family: "PT Root UI"
        font.pixelSize: size * 2
        lineHeightMode: Text.FixedHeight
        lineHeight: size * PillTheme.pillMeetingOfferBodyLineHeight * 2
        scale: 0.5
        transformOrigin: Item.TopLeft
    }

    OverlaySurface {
        objectName: "offerSurface"
        surfaceWidth: root.surfaceWidth
        surfaceHeight: root.surfaceHeight
        surfaceRadius: PillTheme.pillMeetingOfferRadius
        // shadow.notify — same geometry as W3.
        shadowY: 14
        shadowRadius: 36
        shadowColor: Qt.rgba(0, 0, 0, 0.5)
        surfaceColor: PillTheme.notifyBg
        shadowSourceColor: PillTheme.notifyBg
        borderColor: PillTheme.notifyBorder
        borderWidth: root.borderWidth
    }

    Item {
        id: head
        objectName: "offerHead"
        x: root.contentX
        y: root.contentY
        width: root.contentWidth
        height: PillTheme.pillMeetingOfferClose
        Icon {
            objectName: "offerHeadGlyph"
            y: Math.round((parent.height - height) / 2)
            size: PillTheme.pillMeetingOfferHeadIcon
            name: "meeting"
            color: PillTheme.notifyFgMuted
        }
        OfferText {
            id: header
            objectName: "offerHeader"
            x: PillTheme.pillMeetingOfferHeadIcon + PillTheme.pillMeetingOfferHeadGap
            y: Math.round((head.height - lineHeight * scale) / 2)
            size: Theme.notificationHeaderSize
            text: root.texts.header || ""
            color: PillTheme.notifyFgMuted
        }
        MeetingOfferControl {
            objectName: "offerClose"
            anchors.right: parent.right
            closeControl: true
            inert: root.starting
            glyph: "x"
            label: root.texts.close || ""
            Accessible.onPressAction: root.pressed("close", false)
            onPressed: root.pressed("close", mouse)
        }
    }

    Item {
        id: titleBox
        x: root.contentX
        y: head.y + head.height + PillTheme.pillMeetingOfferHeadMarginBottom
        width: root.contentWidth
        height: title.contentHeight * title.scale
        visible: !root.startFailed
        OfferText {
            id: title
            objectName: "offerTitle"
            width: parent.width / scale
            wrapMode: Text.Wrap
            size: Theme.notificationTitleSize
            font.weight: Font.Medium
            text: root.texts.title || ""
            color: PillTheme.notifyFg
        }
    }
    Item {
        id: bodyBox
        x: root.contentX
        y: root.startFailed ? titleBox.y
                            : Math.round(titleBox.y + titleBox.height + PillTheme.pillMeetingOfferTitleMarginBottom)
        width: root.contentWidth
        height: body.contentHeight * body.scale
        OfferText {
            id: body
            objectName: "offerBody"
            width: parent.width / scale
            wrapMode: root.startFailed ? Text.WordWrap : Text.Wrap
            text: root.startFailed ? root.errorText : (root.texts.body || "")
            color: root.startFailed ? PillTheme.notifyFg : PillTheme.pillIconMuted
        }
    }
    Item {
        id: actions
        objectName: "offerActions"
        x: root.contentX
        y: Math.round(bodyBox.y + bodyBox.height + PillTheme.pillMeetingOfferButtonsMarginTop)
        width: root.contentWidth
        height: root.startFailed ? openMeetings.height : start.height
        MeetingOfferControl {
            id: start
            objectName: "offerStart"
            primary: true
            visible: !root.startFailed
            inert: root.starting
            glyph: "rec"
            spinning: root.starting
            freezeAnimations: root.freezeAnimations
            label: (root.starting ? root.texts.starting : root.texts.start) || ""
            Accessible.role: Accessible.StaticText
            onPressed: root.pressed("start", mouse)
        }
        MeetingOfferControl {
            objectName: "offerRefuse"
            x: start.width + Theme.notificationButtonsGap
            visible: !root.startFailed
            inert: root.starting
            label: root.texts.refuse || ""
            Accessible.onPressAction: root.pressed("refuse", false)
            onPressed: root.pressed("refuse", mouse)
        }
        MeetingOfferControl {
            id: openMeetings
            objectName: "offerOpenMeetings"
            visible: root.startFailed
            label: root.texts.openMeetings || ""
            Accessible.onPressAction: root.pressed("open_meetings", false)
            onPressed: root.pressed("open_meetings", mouse)
        }
    }
    Item {
        id: counter
        objectName: "offerCounter"
        x: root.contentX
        y: Math.round(actions.y + actions.height + Theme.notificationButtonsGap)
        width: root.contentWidth
        height: remaining.lineHeight * remaining.scale
        visible: !root.startFailed
        OfferText {
            id: remaining
            objectName: "offerRemaining"
            x: parent.width - implicitWidth * scale
            size: PillTheme.pillMeetingOfferCounterSize
            text: root.remainingText
            color: PillTheme.notifyFgMuted
        }
    }

    // Only the bottom two pixels of the rounded interior are visible.
    Item {
        id: progress
        objectName: "offerProgress"
        property real fraction: 1
        x: root.borderWidth
        y: root.surfaceHeight - root.borderWidth - height
        width: root.surfaceWidth - 2 * root.borderWidth
        height: PillTheme.pillMeetingOfferTimerH
        visible: !root.startFailed
        clip: true
        Item {
            objectName: "offerProgressFill"
            width: parent.width * progress.fraction
            height: parent.height
            clip: true
            Rectangle {
                objectName: "offerProgressColor"
                width: progress.width
                height: 2 * radius
                y: progress.height - height
                radius: PillTheme.pillMeetingOfferRadius - root.borderWidth
                color: PillTheme.notifyBtnPrimaryBg
                antialiasing: true
            }
        }
        NumberAnimation on fraction {
            objectName: "offerCountdown"
            running: root.presented && !root.startFailed && !root.freezeAnimations
            paused: running && root.starting
            from: 1
            to: 0
            duration: 60000
            easing.type: Easing.Linear
        }
    }
}
