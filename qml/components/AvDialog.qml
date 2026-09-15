// design/spec.md §11.1: подтверждение, модальное к родительскому окну.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

Dialog {
    id: root

    property string heading: ""
    property string iconName: ""
    property string message: ""
    property string confirmText: qsTr("Продолжить")
    property bool confirmEnabled: true
    property string cancelText: qsTr("Отмена")
    property string note: ""

    signal confirmed()
    signal cancelled()

    // Controls 2 Dialog — Popup внутри окна, без встроенного свойства modality.
    // modal блокирует только родительское окно (смысл Qt.WindowModal).
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: Theme.dialogW
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    padding: 0
    spacing: 0

    onAccepted: root.confirmed()
    onRejected: root.cancelled()

    background: Rectangle {
        radius: Theme.dialogRadius
        border.width: Theme.borderHairline
        border.color: Theme.border
        color: Theme.bgApp
        antialiasing: true
        // Тень 0 16px 44px из §11.1 опущена.
    }

    header: Item {
        implicitHeight: Theme.dialogHeaderH

        Rectangle {
            anchors.fill: parent
            anchors.margins: Theme.borderHairline
            anchors.bottomMargin: 0
            radius: Theme.dialogRadius - Theme.borderHairline
            color: Theme.bgTitlebar
            antialiasing: true

            Rectangle {
                width: parent.width
                height: parent.radius
                y: parent.height - height
                color: Theme.bgTitlebar
            }
        }

        Rectangle {
            width: parent.width
            height: Theme.borderHairline
            y: parent.height - height
            color: Theme.border
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 10 // design/spec.md §11.1: паддинг шапки.
            anchors.rightMargin: 10 // design/spec.md §11.1: паддинг шапки.
            spacing: 7 // design/spec.md §11.1: зазор в шапке.

            Icon {
                name: root.iconName
                visible: name !== ""
                size: 14 // Макет 02-models-file-dialogs.html: .dh svg.
                color: Theme.fgMuted
                Layout.alignment: Qt.AlignVCenter
            }

            Text {
                text: root.heading
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontTitlebarSize
                color: Theme.fgSecondary
                textFormat: Text.PlainText
                elide: Text.ElideRight
                renderType: Text.NativeRendering
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    contentItem: Item {
        implicitHeight: Theme.dialogBodyPaddingTop + bodyLine.implicitHeight + Theme.dialogBodyPaddingBottom

        RowLayout {
            id: bodyLine
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            width: Math.max(0, parent.width - Theme.dialogBodyPaddingX * 2)
            spacing: Theme.dialogBodyGap

            Icon {
                name: root.iconName
                visible: name !== ""
                size: Theme.noteBannerIcon
                color: Theme.fgMuted
                Layout.alignment: Qt.AlignTop
            }

            Text {
                text: root.message
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontBodySize
                lineHeight: Theme.fontBodySize * Theme.fontBodyLineHeight
                lineHeightMode: Text.FixedHeight
                color: Theme.fg
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                renderType: Text.NativeRendering
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
            }
        }
    }

    footer: Item {
        implicitHeight: Theme.dialogFooterPaddingTop + footerLine.implicitHeight + Theme.dialogFooterPaddingBottom

        RowLayout {
            id: footerLine
            x: Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop
            width: Math.max(0, parent.width - Theme.dialogFooterPaddingX * 2)
            spacing: Theme.dialogFooterGap

            Text {
                text: root.note
                visible: text !== ""
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                color: Theme.fgMuted
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                renderType: Text.NativeRendering
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
            }

            Item { Layout.fillWidth: true }

            AvButton {
                text: root.cancelText
                variant: "secondary"
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.reject()
            }

            AvButton {
                text: root.confirmText
                variant: "primary"
                enabled: root.confirmEnabled
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.accept()
            }
        }
    }
}
