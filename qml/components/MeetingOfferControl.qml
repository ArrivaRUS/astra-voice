import QtQuick 2.15
import ".."

Rectangle {
    id: control
    property string label
    property string glyph
    property bool primary: false
    property bool closeControl: false
    readonly property bool hasActionGlyph: !closeControl && glyph.length > 0
    readonly property real glyphSpace: hasActionGlyph ? actionGlyph.width + gap : 0
    readonly property real gap: PillTheme.pillMeetingOfferHeadMarginBottom
    readonly property color labelColor: primary ? PillTheme.notifyBtnPrimaryFg : PillTheme.notifyFg
    readonly property bool hovered: pointer.containsMouse
    signal pressed(bool mouse)

    width: closeControl ? PillTheme.pillMeetingOfferClose
                        : labelBox.width + glyphSpace + 2 * Theme.notificationButtonPaddingX
    height: closeControl ? width : Math.round(labelBox.height + 2 * Theme.notificationButtonPaddingY)
    radius: closeControl ? width / 2 : Theme.notificationButtonRadius
    color: primary ? PillTheme.notifyBtnPrimaryBg : PillTheme.notifyBtnBg
    antialiasing: true
    Accessible.role: Accessible.Button
    Accessible.name: control.label
    Accessible.onPressAction: control.pressed(false)

    Icon {
        objectName: "offerCloseGlyph"
        anchors.centerIn: parent
        visible: control.closeControl
        name: visible ? control.glyph : ""
        size: PillTheme.pillMeetingOfferCloseIcon
        color: PillTheme.pillCloseFg
    }
    Icon {
        id: actionGlyph
        objectName: "offerActionGlyph"
        x: Theme.notificationButtonPaddingX
        y: Math.round((parent.height - height) / 2)
        visible: control.hasActionGlyph
        name: control.hasActionGlyph ? control.glyph : ""
        size: PillTheme.pillMeetingOfferBtnIcon
        fill: true
        color: control.labelColor
    }
    Item {
        id: labelBox
        x: Theme.notificationButtonPaddingX + control.glyphSpace
        y: Math.round((parent.height - height) / 2)
        visible: !control.closeControl
        width: labelText.implicitWidth * labelText.scale
        height: Math.round(labelText.lineHeight * labelText.scale)
        Text {
            id: labelText
            objectName: "offerActionLabel"
            text: control.label
            textFormat: Text.PlainText
            color: control.labelColor
            font.family: "PT Root UI"
            font.pixelSize: PillTheme.pillMeetingOfferBodySize * 2
            font.weight: control.primary ? Font.Medium : Font.Normal
            lineHeightMode: Text.FixedHeight
            lineHeight: PillTheme.pillMeetingOfferBodySize * PillTheme.pillMeetingOfferBodyLineHeight * 2
            scale: 0.5
            transformOrigin: Item.TopLeft
        }
    }
    MouseArea {
        id: pointer
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onPressed: control.pressed(true)
    }
}
