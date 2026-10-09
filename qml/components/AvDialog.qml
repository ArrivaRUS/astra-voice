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
    // Opt-in для длинных подтверждений; прежние диалоги сохраняют геометрию.
    property bool safeConfirmation: false
    property Item returnFocusItem: null
    property Item fallbackFocusItem: null

    signal confirmed()
    signal cancelled()

    // Controls 2 Dialog — Popup внутри окна, без встроенного свойства modality.
    // modal блокирует только родительское окно (смысл Qt.WindowModal).
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    width: safeConfirmation && parent ? Math.min(Theme.dialogW, parent.width - 24) : Theme.dialogW
    height: safeConfirmation && parent ? Math.min(implicitHeight, parent.height - 24) : implicitHeight
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    padding: 0
    spacing: 0

    onAccepted: root.confirmed()
    onRejected: root.cancelled()

    function restoreReturnFocus() {
        if (root.visible || !root.safeConfirmation) return
        var target = root.returnFocusItem
        if (!target || !target.enabled || !target.visible) target = root.fallbackFocusItem
        if (target && target.enabled && target.visible) target.forceActiveFocus(Qt.TabFocusReason)
    }

    function scrollBody(key) {
        if (!root.safeConfirmation) return false
        var body = root.contentItem
        var end = Math.max(0, body.contentHeight - body.height)
        var page = Math.max(1, body.height * 0.9)
        switch (key) {
        case Qt.Key_PageDown: body.contentY = Math.min(end, body.contentY + page); break
        case Qt.Key_PageUp: body.contentY = Math.max(0, body.contentY - page); break
        case Qt.Key_Home: body.contentY = 0; break
        case Qt.Key_End: body.contentY = end; break
        default: return false
        }
        return true
    }

    Connections {
        target: root
        function onOpened() {
            if (root.safeConfirmation)
                (cancelButton.visible ? cancelButton : confirmButton).forceActiveFocus(Qt.TabFocusReason)
        }
        function onClosed() {
            if (root.safeConfirmation) Qt.callLater(root.restoreReturnFocus)
        }
    }

    background: Rectangle {
        radius: Theme.dialogRadius
        color: Theme.bgApp
        antialiasing: true
        // Тень 0 16px 44px из §11.1 опущена.

        // Обводка рисуется поверх заливки: в тёмной теме Theme.border — 10 % белого.
        // Композит считаем от заливки диалога, иначе подложка делает рамку темнее макета.
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: "transparent"
            border.width: Theme.borderHairline
            border.color: Theme.border
            antialiasing: true
        }
    }

    header: Item {
        implicitHeight: root.safeConfirmation
            ? Math.max(Theme.dialogHeaderH, headingText.implicitHeight + 10) : Theme.dialogHeaderH

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
                id: headingText
                text: root.heading
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontTitlebarSize
                color: Theme.fgSecondary
                textFormat: Text.PlainText
                elide: root.safeConfirmation ? Text.ElideNone : Text.ElideRight
                wrapMode: root.safeConfirmation ? Text.Wrap : Text.NoWrap
                renderType: Text.NativeRendering
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    contentItem: Flickable {
        objectName: "dialogBody"
        Accessible.role: Accessible.Dialog
        Accessible.name: root.heading
        Accessible.description: root.message
        implicitHeight: Theme.dialogBodyPaddingTop
            + Math.max(bodyText.paintedHeight, root.iconName !== "" ? Theme.noteBannerIcon : 0)
            + Theme.dialogBodyPaddingBottom
        contentHeight: implicitHeight
        contentWidth: width
        clip: true
        interactive: root.safeConfirmation && contentHeight > height
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: root.safeConfirmation ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }

        Icon {
            id: bodyIcon
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            name: root.iconName
            visible: name !== ""
            size: Theme.noteBannerIcon
            color: Theme.fgMuted
        }

        Text {
            id: bodyText
            objectName: "dialogMessage"
            x: Theme.dialogBodyPaddingX + (bodyIcon.visible
                ? Theme.noteBannerIcon + Theme.dialogBodyGap : 0)
            y: Theme.dialogBodyPaddingTop
            width: Math.max(0, root.width - x - Theme.dialogBodyPaddingX)
            text: root.message
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBodySize
            lineHeight: (root.safeConfirmation ? font.pixelSize : Theme.fontBodySize) * Theme.fontBodyLineHeight
            lineHeightMode: Text.FixedHeight
            color: Theme.fg
            textFormat: Text.PlainText
            wrapMode: root.safeConfirmation ? Text.Wrap : Text.WordWrap
            renderType: Text.NativeRendering
        }
    }

    footer: Item {
        implicitHeight: Theme.dialogFooterPaddingTop
            + (root.safeConfirmation ? safeFooter.bodyHeight : footerLine.implicitHeight)
            + Theme.dialogFooterPaddingBottom

        // Старые короткие диалоги сохраняют прежнюю раскладку.
        RowLayout {
            id: footerLine
            visible: !root.safeConfirmation
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
                visible: root.cancelText !== ""
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

        Item {
            id: safeFooter
            visible: root.safeConfirmation
            x: Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop
            width: Math.max(0, root.width - Theme.dialogFooterPaddingX * 2)
            height: bodyHeight
            readonly property bool stacked: cancelButton.visible
                && cancelButton.implicitWidth + confirmButton.implicitWidth + Theme.dialogFooterGap > width
            readonly property real bodyHeight: stacked
                ? cancelButton.height + Theme.dialogFooterGap + confirmButton.height
                : Math.max(cancelButton.visible ? cancelButton.height : 0, confirmButton.height)

            AvButton {
                id: cancelButton
                objectName: "dialogCancel"
                text: root.cancelText
                visible: root.cancelText !== ""
                variant: "secondary"
                x: safeFooter.stacked ? 0 : confirmButton.x - width - Theme.dialogFooterGap
                y: 0
                width: safeFooter.stacked ? safeFooter.width : Math.min(implicitWidth, safeFooter.width)
                implicitWidth: cancelTextItem.implicitWidth + leftPadding + rightPadding
                implicitHeight: Math.max(Theme.buttonHeight,
                    cancelTextItem.implicitHeight + topPadding + bottomPadding)
                contentItem: Text {
                    id: cancelTextItem
                    text: cancelButton.text
                    textFormat: Text.PlainText
                    font: cancelButton.font
                    color: cancelButton.fgColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }
                KeyNavigation.tab: confirmButton.enabled ? confirmButton : cancelButton
                KeyNavigation.backtab: confirmButton.enabled ? confirmButton : cancelButton
                Keys.onPressed: event.accepted = root.scrollBody(event.key)
                Keys.onReturnPressed: root.reject()
                Keys.onEnterPressed: root.reject()
                Keys.onEscapePressed: root.reject()
                onClicked: root.reject()
            }
            AvButton {
                id: confirmButton
                objectName: "dialogConfirm"
                text: root.confirmText
                variant: "primary"
                enabled: root.confirmEnabled
                x: safeFooter.width - width
                y: safeFooter.stacked ? cancelButton.height + Theme.dialogFooterGap : 0
                width: safeFooter.stacked ? safeFooter.width : Math.min(implicitWidth, safeFooter.width)
                implicitWidth: confirmTextItem.implicitWidth + leftPadding + rightPadding
                implicitHeight: Math.max(Theme.buttonHeight,
                    confirmTextItem.implicitHeight + topPadding + bottomPadding)
                contentItem: Text {
                    id: confirmTextItem
                    text: confirmButton.text
                    textFormat: Text.PlainText
                    font: confirmButton.font
                    color: confirmButton.fgColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }
                KeyNavigation.tab: cancelButton.visible ? cancelButton : confirmButton
                KeyNavigation.backtab: cancelButton.visible ? cancelButton : confirmButton
                Keys.onPressed: event.accepted = root.scrollBody(event.key)
                Keys.onReturnPressed: if (enabled) root.accept()
                Keys.onEnterPressed: if (enabled) root.accept()
                Keys.onEscapePressed: root.reject()
                onClicked: root.accept()
            }
        }
    }
}
