// design/spec.md §4.5, §7: поле захвата и семь состояний назначения.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Item {
    id: root

    property string state7: "idle"
    property string hotkey: qsTr("Ctrl + Space")
    property string conflictOwner: ""

    signal changeRequested()
    signal cancelRequested()
    signal keepRequested()
    signal chooseAnotherRequested()
    signal toggleModeRequested()
    signal retryRequested()

    readonly property bool captureActive: state7 === "capturing" || state7 === "captured"
    readonly property bool hasResult: state7 === "success" || state7 === "conflict"
        || state7 === "duplicate" || state7 === "not-grabbed"

    function escPressed() {
        if (!captureActive)
            return false
        cancelRequested()
        return true
    }

    implicitWidth: Math.max(idleRow.implicitWidth, captureLine.implicitWidth + Theme.fieldPaddingX * 2)
    implicitHeight: content.implicitHeight
    width: parent ? parent.width : implicitWidth
    height: implicitHeight

    Keys.onEscapePressed: event.accepted = root.escPressed()

    Column {
        id: content
        width: root.width
        spacing: 8 // design/spec.md §7: ряд и отступ перед баннером.

        RowLayout {
            id: idleRow
            visible: root.state7 === "idle" || root.hasResult
            height: visible ? implicitHeight : 0
            spacing: 8 // design/spec.md §7: чип + «Изменить».

            KeyChip {
                text: root.hotkey
                Layout.alignment: Qt.AlignVCenter
            }

            AvButton {
                text: qsTr("Изменить")
                small: true
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.changeRequested()
            }
        }

        Rectangle {
            id: field
            width: parent.width
            implicitHeight: captureLine.implicitHeight + Theme.fieldPaddingY * 2
            height: visible ? implicitHeight : 0
            visible: root.captureActive
            radius: Theme.fieldRadius
            border.width: Theme.fieldBorder
            border.color: Theme.primary
            color: Theme.bgSurface
            antialiasing: true

            // CSS box-shadow лежит снаружи поля, не участвуя в раскладке.
            Rectangle {
                anchors.fill: parent
                anchors.margins: -2 // design/spec.md §4.5: кольцо 0 0 0 2px.
                radius: Theme.fieldRadius + 2 // design/spec.md §4.5: внешний радиус кольца.
                color: "transparent"
                border.width: 2 // design/spec.md §4.5: толщина кольца.
                border.color: Theme.primaryBg
                antialiasing: true
            }

            RowLayout {
                id: captureLine
                x: Theme.fieldPaddingX
                y: Theme.fieldPaddingY
                width: Math.max(0, field.width - Theme.fieldPaddingX * 2)
                spacing: Theme.fieldGap

                Text {
                    text: root.state7 === "captured" ? root.hotkey : qsTr("Нажмите комбинацию…")
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.fontHotkeyCaptureSize
                    color: Theme.fg
                    textFormat: Text.PlainText
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: root.state7 === "captured" ? qsTr("Отпустите клавиши") : qsTr("Esc — отмена")
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontCaptionSize
                    color: Theme.fgMuted
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }

        NoteBanner {
            visible: root.state7 === "success"
            height: visible ? implicitHeight : 0
            variant: "ok"
            iconName: "check"
            title: qsTr("Комбинация назначена")
            body: qsTr("Теперь диктовка работает по %1.").arg(root.hotkey)
        }

        NoteBanner {
            visible: root.state7 === "conflict"
            height: visible ? implicitHeight : 0
            variant: "warn"
            iconName: "alert"
            title: qsTr("Комбинация занята в KDE")
            body: root.conflictOwner.trim() !== ""
                ? qsTr("%1 уже назначена на действие «%2». Оставить её можно, но диктовка может не сработать — система заберёт нажатие себе.").arg(root.hotkey).arg(root.conflictOwner)
                : qsTr("%1 уже занята другой программой. Оставить её можно, но диктовка может не сработать — система заберёт нажатие себе.").arg(root.hotkey)

            AvButton {
                text: qsTr("Оставить %1").arg(root.hotkey)
                variant: "secondary"
                onClicked: root.keepRequested()
            }

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }
        }

        NoteBanner {
            visible: root.state7 === "duplicate"
            height: visible ? implicitHeight : 0
            variant: "warn"
            iconName: "alert"
            title: qsTr("Эта комбинация уже назначена")
            body: qsTr("Она занята другой настройкой Astra Voice. Выберите другое сочетание или измените режим.")

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }

            AvButton {
                text: qsTr("Открыть „нажать-нажать“")
                variant: "ghost"
                onClicked: root.toggleModeRequested()
            }
        }

        NoteBanner {
            visible: root.state7 === "not-grabbed"
            height: visible ? implicitHeight : 0
            variant: "error"
            iconName: "alert"
            title: qsTr("Горячая клавиша не захвачена")
            body: qsTr("Другая программа держит это сочетание. Диктовка не заработает, пока не выбрано свободное.")

            AvButton {
                text: qsTr("Выбрать другую")
                variant: "primary"
                onClicked: root.chooseAnotherRequested()
            }

            AvButton {
                text: qsTr("Повторить попытку")
                variant: "secondary"
                onClicked: root.retryRequested()
            }
        }
    }
}
