// Раздел «О программе» — референс design/refs/06-about.png (+ -dark), спека §3.1–3.2.
// Пока здесь только версия и две честные строки: остальное (лицензии, данные на
// диске, статистика) появится вместе с этими экранами.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Column {
    id: root

    readonly property var info: (typeof appInfo !== "undefined" && appInfo !== null) ? appInfo : null
    readonly property string appVersion: (root.info && root.info.version) ? root.info.version : "0.1.0"

    spacing: Theme.spaceGroupGap

    SettingGroup {
        width: root.width
        title: qsTr("Версия")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Astra Voice")
            sub: qsTr("Голосовой ввод для Astra Linux")
            showHint: false

            Text {
                textFormat: Text.PlainText
                text: root.appVersion
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                renderType: Text.NativeRendering
                elide: Text.ElideRight
                Layout.maximumWidth: root.width / 3
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    SettingGroup {
        width: root.width
        title: qsTr("Лицензии и приватность")

        SettingRow {
            width: parent.width
            divider: false
            label: qsTr("Приватность")
            sub: qsTr("Программа не выходит в сеть без вашего действия")
            showHint: false
        }

        SettingRow {
            width: parent.width
            label: qsTr("Лицензия")
            sub: qsTr("Исходный код открыт")
            showHint: false
        }
    }
}
