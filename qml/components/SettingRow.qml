// Строка настройки — design/spec.md §3.2.
// Порядок слева направо: подпись и пояснение → «?» → бейдж замка → контрол.
// Строка С ТУМБЛЕРОМ кликабельна целиком и подсвечивается на hover (решение У5);
// строка со списком, кнопкой или полем реагирует только на сам контрол.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Item {
    id: root

    property string label: ""
    property string sub: ""
    property bool showHint: true
    // Разделитель сверху: ставится у каждой строки, кроме первой в карточке (§3.1).
    property bool divider: true
    // Строка целиком кликабельна и переключает этот тумблер (У5).
    property var toggle: null
    // Вид «выключено»: только цвет fg-disabled, без прозрачности (сквозное правило 2).
    property bool rowEnabled: true
    // Бейдж «Задано администратором» (§3.2).
    property bool locked: false
    property string lockedText: ""

    default property alias controlData: slot.data

    readonly property color labelColor: rowEnabled ? Theme.fg : Theme.fgDisabled
    readonly property color subColor: rowEnabled ? Theme.fgMuted : Theme.fgDisabled

    // Высота — по содержимому в паддингах 7/14 (§3.2): с пояснением `card.row-h-with-sub`,
    // без пояснения не меньше `card.row-min-h`, со списком — по высоте контрола.
    // Округляем до целого: грани 1 px не должны попадать на субпиксель.
    // Макет 08-onboarding-1-network.html (.card>.r+.r): верхний разделитель добавляет 1 px к строке.
    implicitHeight: Math.ceil(Math.max(
        Theme.cardRowMinH,
        line.implicitHeight + Theme.cardRowPaddingY * 2,
        slot.implicitHeight + Theme.cardRowPaddingY * 2,
        sub !== "" ? Theme.cardRowHWithSub : 0)) + (divider ? Theme.spaceCardRowDivider : 0)
    height: implicitHeight

    // Подсветка строки при наведении — без анимации (см. SidebarItem).
    Rectangle {
        anchors.fill: parent
        color: (root.toggle && root.rowEnabled && hoverArea.containsMouse)
               ? Theme.stateHoverOnSurface : "transparent"
    }

    Rectangle {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Theme.spaceCardRowDivider
        color: Theme.borderSoft
        visible: root.divider
    }

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: root.toggle !== null
        enabled: root.toggle !== null && root.rowEnabled
        onClicked: if (root.toggle) root.toggle.toggle()
    }

    RowLayout {
        id: line
        anchors.fill: parent
        anchors.leftMargin: Theme.cardRowPaddingX
        anchors.rightMargin: Theme.cardRowPaddingX
        // Макет 08-onboarding-1-network.html (.card>.r+.r): паддинг начинается после разделителя.
        anchors.topMargin: Theme.cardRowPaddingY + (root.divider ? Theme.spaceCardRowDivider : 0)
        anchors.bottomMargin: Theme.cardRowPaddingY
        spacing: Theme.statusbarGap

        // Column считает высоту текста без повторного расчёта вложенного Layout в Qt5.
        Column {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 2

            Text {
                width: parent.width
                verticalAlignment: Text.AlignVCenter
                textFormat: Text.PlainText
                text: root.label
                color: root.labelColor
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingLabelSize
                // Межстрочный из токенов задан относительно РАЗМЕРА шрифта (как CSS),
                // а ProportionalHeight в QML множит высоту строки шрифта — отсюда FixedHeight.
                lineHeight: Math.round(Theme.fontSettingLabelSize * Theme.fontSettingLabelLineHeight)
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }

            Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.sub
                visible: root.sub !== ""
                color: root.subColor
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }
        }

        // Значок «?» — 15 × 15, круг, граница 1 px fg-faint, текст 10 / 500 (§3.2).
        Rectangle {
            Layout.preferredWidth: Theme.hintSize
            Layout.preferredHeight: Theme.hintSize
            Layout.alignment: Qt.AlignVCenter
            radius: Theme.hintSize / 2
            antialiasing: true
            color: "transparent"
            border.width: Theme.borderHairline
            border.color: Theme.fgFaint
            visible: root.showHint

            Text {
                anchors.centerIn: parent
                text: "?"
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: 10  // §3.2: текст значка «?» — 10 / 500
                font.weight: Font.Medium
                renderType: Text.NativeRendering
            }
        }

        // Бейдж «Задано администратором» (§3.2): замок + подпись, не только цвет.
        Rectangle {
            Layout.preferredWidth: lockRow.implicitWidth + Theme.lockBadgePaddingX * 2
            Layout.preferredHeight: Theme.badgeHeight
            Layout.alignment: Qt.AlignVCenter
            radius: Theme.lockBadgeRadius
            antialiasing: true
            color: Theme.lockBadgeBg
            visible: root.locked

            RowLayout {
                id: lockRow
                anchors.centerIn: parent
                spacing: Theme.badgeGap

                Icon {
                    name: "lock"
                    size: Theme.lockBadgeIcon
                    color: Theme.lockBadgeFg
                    Layout.alignment: Qt.AlignVCenter
                }

                Text {
                    textFormat: Text.PlainText
                    text: root.lockedText !== "" ? root.lockedText : qsTr("Задано администратором")
                    color: Theme.lockBadgeFg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.badgeSize
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }

        RowLayout {
            id: slot
            Layout.alignment: Qt.AlignVCenter
            spacing: Theme.fieldGap
        }
    }
}
