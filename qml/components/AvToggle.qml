// Тумблер — design/spec.md §4.2. Габарит 38 × 21, ручка 17 белая, ход 120 мс.
// «Заблокирован» сохраняет своё положение цветом: включённый — `primary`, выключенный —
// `border`. Приглушения прозрачностью нет (сквозное правило 2, `toggle.locked-opacity` = 1):
// недоступность несут цвет подписи строки и текст рядом («Выключить нельзя»).
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Switch {
    id: control

    // Заблокирован политикой: рядом обязателен бейдж с замком — цвет не единственный носитель.
    property bool locked: false

    padding: 0
    implicitWidth: Theme.toggleW
    implicitHeight: Theme.toggleH
    enabled: !locked

    // Цвет дорожки переключается мгновенно; анимируется только ход ручки.
    indicator: Rectangle {
        width: Theme.toggleW
        height: Theme.toggleH
        radius: Theme.toggleRadius
        antialiasing: true
        opacity: control.locked ? Theme.toggleLockedOpacity : 1
        color: {
            if (control.locked)
                return control.checked ? Theme.toggleLockedOnBg : Theme.toggleLockedOffBg;
            return control.checked ? Theme.toggleOnBg : Theme.toggleOffBg;
        }

        Rectangle {
            y: Theme.toggleKnobInset
            x: control.checked ? Theme.toggleKnobOnX : Theme.toggleKnobInset
            width: Theme.toggleKnob
            height: Theme.toggleKnob
            radius: width / 2
            color: Theme.toggleKnobBg
            antialiasing: true

            Behavior on x {
                NumberAnimation {
                    duration: Theme.durationHover
                    easing.type: Easing.Bezier
                    easing.bezierCurve: Theme.easingHover.concat([1, 1])
                }
            }
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

    contentItem: Item {}
}
