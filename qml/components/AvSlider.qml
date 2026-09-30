// Ползунок системной громкости микрофона, шаг 5 %.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Slider {
    id: control

    from: 0
    to: 100
    stepSize: 5
    snapMode: Slider.SnapAlways
    padding: 0
    implicitWidth: Theme.progressStatusbarW  // компактная ширина дорожки из темы
    implicitHeight: Theme.toggleH

    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        width: control.availableWidth
        height: Theme.spaceRowGap
        radius: height / 2
        color: Theme.toggleOffBg
        antialiasing: true

        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: parent.radius
            color: Theme.toggleOnBg
            antialiasing: true
        }

        Rectangle {
            anchors.fill: parent
            anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
            radius: Theme.focusRadius
            color: "transparent"
            border.width: Theme.focusWidth
            border.color: Theme.stateFocusRing
            antialiasing: true
            visible: control.visualFocus
        }
    }

    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + (control.availableHeight - height) / 2
        width: Theme.toggleKnob
        height: Theme.toggleKnob
        radius: width / 2
        color: Theme.toggleKnobBg
        border.width: Theme.borderHairline
        border.color: Theme.border
        antialiasing: true
    }
}
