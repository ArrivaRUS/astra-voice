// Скрытый раздел «Отладка» — design/refs/07-debug.png.
// Пока здесь одно действие: перезапуск звуковой службы сеанса — только по нажатию.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    readonly property var settings: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null

    spacing: Theme.spaceGroupGap

    SettingGroup {
        width: root.width
        title: qsTr("Звук")
        visible: root.settings ? root.settings.canRestartSoundService : false
        height: visible ? implicitHeight : 0

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Перезапустить звуковую службу")
            sub: qsTr("Помогает, когда микрофон перестал работать после входа в систему")

            AvButton {
                text: qsTr("Перезапустить")
                small: true
                Layout.alignment: Qt.AlignVCenter
                onClicked: {
                    if (root.settings)
                        root.settings.restartSoundService()
                }
            }
        }
    }

    Stub {
        width: root.width
        note: qsTr("Здесь будут сведения для поддержки")
    }
}
