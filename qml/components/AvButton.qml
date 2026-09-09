// Кнопка — design/spec.md §4.1. Варианты: secondary (по умолчанию), primary, ghost;
// размер sm. Разрушающего варианта нет (решение В5). disabled — только цветом, opacity 1.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

Button {
    id: control

    // "secondary" | "primary" | "ghost"
    property string variant: "secondary"
    property bool small: false
    property string iconName: ""

    readonly property bool ghost: variant === "ghost"
    readonly property bool primary: variant === "primary"
    readonly property color fgColor: !enabled ? Theme.fgDisabled
        : (primary ? Theme.primaryFg : (ghost ? Theme.primary : Theme.fg))

    padding: 0
    leftPadding: small ? Theme.buttonSmPaddingX : (ghost ? Theme.buttonGhostPaddingX : Theme.buttonPaddingX)
    rightPadding: leftPadding
    topPadding: small ? Theme.buttonSmPaddingY : (ghost ? Theme.buttonGhostPaddingY : Theme.buttonPaddingY)
    bottomPadding: topPadding
    implicitHeight: small ? Theme.buttonHeightSm : Theme.buttonHeight
    implicitWidth: contentRow.implicitWidth + leftPadding + rightPadding
    font.family: Theme.fontUi
    font.pixelSize: small ? Theme.fontButtonSmSize : Theme.fontButtonSize
    font.weight: Font.Medium

    background: Rectangle {
        radius: Theme.buttonRadius
        border.width: control.ghost ? 0 : Theme.buttonBorder
        border.color: control.enabled ? (control.primary ? Theme.primary : Theme.border) : Theme.border
        color: {
            if (!control.enabled)
                return control.ghost ? "transparent" : Theme.bgSurface2;
            if (control.primary)
                return control.pressed ? Theme.statePrimaryPressed
                     : (control.hovered ? Theme.statePrimaryHover : Theme.primary);
            if (control.pressed)
                return Theme.statePressedOnSurface;
            if (control.hovered)
                return Theme.stateHoverOnSurface;
            return control.ghost ? "transparent" : Theme.bgSurface;
        }

        Behavior on color {
            ColorAnimation {
                duration: Theme.durationHover
                easing.type: Easing.Bezier
                easing.bezierCurve: Theme.easingHover.concat([1, 1])
            }
        }

        // Кольцо фокуса: 2 px снаружи, зазор 2, радиус 8 (сквозное правило 1).
        Rectangle {
            anchors.fill: parent
            anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
            radius: Theme.focusRadius
            color: "transparent"
            border.width: Theme.focusWidth
            border.color: Theme.stateFocusRing
            visible: control.visualFocus
        }
    }

    contentItem: RowLayout {
        id: contentRow
        spacing: control.iconName === "" ? 0 : Theme.buttonGap

        Icon {
            name: control.iconName
            size: Theme.buttonIconSize
            color: control.fgColor
            visible: control.iconName !== ""
            Layout.alignment: Qt.AlignVCenter
        }

        Text {
            text: control.text
            font: control.font
            renderType: Text.NativeRendering
            color: control.fgColor
            Layout.alignment: Qt.AlignVCenter
        }
    }
}
