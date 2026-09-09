// Раздел «Общие» — референс design/refs/01-general-base.png (+ -dark), спека §3.
// Семь настроек в трёх группах, помещаются без прокрутки. Значения — статические
// дефолты M1: настоящие придут из core/settings.py в M3.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    spacing: Theme.spaceGroupGap

    SettingGroup {
        width: root.width
        title: qsTr("Диктовка")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Горячая клавиша")
            sub: qsTr("Удерживайте и говорите — текст появится там, где курсор")

            KeyChip {
                text: "Ctrl + Space"
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                text: qsTr("Изменить")
                small: true
                Layout.alignment: Qt.AlignVCenter
            }
        }

        SettingRow {
            width: parent.width
            label: qsTr("Режим")
            sub: qsTr("Удерживать — самый предсказуемый вариант")

            AvSegmented {
                options: [qsTr("Удерживать"), qsTr("Нажать-нажать")]
                currentIndex: 0
                Layout.alignment: Qt.AlignVCenter
            }
        }

        SettingRow {
            width: parent.width
            // Пояснения нет: техническое имя карты пользователю не нужно.
            // В M3 сюда пойдёт только человекочитаемое device.description из PulseAudio.
            label: qsTr("Микрофон")

            AvSelect {
                Layout.preferredWidth: 236  // §4.4: типовая ширина списка в строке настройки
                Layout.alignment: Qt.AlignVCenter
                model: [qsTr("Системный по умолчанию")]
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Индикация")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Индикатор записи")
            sub: qsTr("Пилюля не забирает фокус и не появляется в Alt+Tab")

            AvSelect {
                Layout.preferredWidth: 236
                Layout.alignment: Qt.AlignVCenter
                model: [qsTr("Пилюля снизу экрана"), qsTr("Только значок в трее")]
            }
        }

        SettingRow {
            width: parent.width
            label: qsTr("Звук начала и конца записи")
            toggle: soundToggle

            AvToggle {
                id: soundToggle
                checked: false
                Layout.alignment: Qt.AlignVCenter
            }
        }

        // Выключить нельзя — иначе запись стала бы скрытой (PRD §9.5).
        // Запрет показан ПОДПИСЬЮ, а не только серым цветом (сквозное правило 3).
        SettingRow {
            width: parent.width
            label: qsTr("Значок в системном трее")
            sub: qsTr("Запись всегда видна — это требование приватности")
            rowEnabled: false

            Text {
                text: qsTr("Выключить нельзя")
                // Подпись объясняет запрет и должна читаться: fg-muted, а не fg-disabled.
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            // Включён и заблокирован: положение читается цветом primary, запрет — подписью.
            AvToggle {
                checked: true
                locked: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Запуск")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Автозапуск при входе в систему")
            toggle: autostartToggle

            AvToggle {
                id: autostartToggle
                checked: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }
}
