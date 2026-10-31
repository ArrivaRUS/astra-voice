// D1: design/meetings/spec.md §3.2/4.2; long body scrolls, footer stays fixed.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

AvDialog {
    id: root
    objectName: "meetingResponsibility"
    property var bridge: null
    property bool offerChecked: false
    readonly property var fallbackItems: [
        { title: "Что записывается", text: "Ваш микрофон и звук собеседников из программы видеосвязи. Из записи программа делает текст — на этом компьютере, без интернета.", icon: "meeting", warning: false },
        { title: "Когда", text: "Только после того, как вы нажмёте «Начать запись». Пока идёт запись, на экране видна плашка с таймером — скрыть её нельзя, остановить можно одним нажатием.", icon: "rec", warning: false },
        { title: "Где хранится", text: "Только на этом компьютере. Звук удаляется сразу после расшифровки, если вы не выберете иначе. Встречу можно удалить целиком в любой момент.", icon: "home", warning: false },
        { title: "Предупреждать участников — ваша обязанность", text: "При каждом начале записи программа покажет готовый текст для чата встречи — скопируйте его и скажите вслух. Лучше предупреждать заранее — например, в приглашении на встречу.", icon: "alert", warning: true },
        { title: "Только свои встречи", text: "Записывайте только встречи, в которых участвуете сами, и не передавайте запись другим без согласия участников.", icon: "people", warning: false }
    ]
    function txt(key, fallback) { return bridge && bridge.texts[key] !== undefined ? bridge.texts[key] : fallback }
    heading: txt("d1Title", "Запись встреч")
    brandHeader: true
    safeConfirmation: false
    width: parent ? Math.min(Theme.responsibilityDialogW, parent.width - 48) : Theme.responsibilityDialogW
    height: parent ? Math.min(implicitHeight, Math.max(1, parent.height - Theme.responsibilityDialogMaxHInset)) : implicitHeight
    visible: bridge ? bridge.responsibilityOpen : false
    onOpened: {
        scroller.contentY = 0
        offerChecked = false
        cancel.forceActiveFocus(Qt.TabFocusReason)
    }
    onCancelled: if (bridge) bridge.cancelResponsibility()
    onConfirmed: if (bridge) bridge.acceptResponsibility(offerChecked)
    contentItem: Flickable {
        id: scroller
        objectName: "meetingResponsibilityBody"
        contentWidth: width
        contentHeight: column.implicitHeight + Theme.dialogBodyPaddingTop + Theme.dialogBodyPaddingBottom
        implicitHeight: contentHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
            id: column
            x: Theme.dialogBodyPaddingX
            y: Theme.dialogBodyPaddingTop
            width: parent.width - Theme.dialogBodyPaddingX * 2
            spacing: Theme.responsibilityDialogBodyGap
            Text {
                text: root.txt("d1Heading", "Перед тем как включить")
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontH3SubsectionSize
                font.weight: Font.Medium
                color: Theme.fg
                renderType: Text.NativeRendering
            }
            Repeater {
                model: root.bridge && root.bridge.responsibilityItems.length ? root.bridge.responsibilityItems : root.fallbackItems
                delegate: Item {
                    width: column.width
                    height: itemText.implicitHeight + (modelData.warning ? Theme.responsibilityDialogWarnPaddingY * 2 : 0)
                    Rectangle {
                        x: -Theme.responsibilityDialogWarnBleed
                        width: parent.width + Theme.responsibilityDialogWarnBleed * 2
                        height: parent.height
                        radius: Theme.responsibilityDialogWarnRadius
                        color: Theme.warningBg
                        visible: modelData.warning
                    }
                    Icon {
                        x: 0
                        y: Theme.responsibilityDialogIconOffsetTop + (modelData.warning ? Theme.responsibilityDialogWarnPaddingY : 0)
                        name: modelData.icon
                        fill: modelData.icon === "rec"
                        size: Theme.responsibilityDialogIcon
                        color: modelData.warning ? Theme.warningInk : Theme.fgMuted
                    }
                    Column {
                        id: itemText
                        x: Theme.responsibilityDialogIcon + Theme.responsibilityDialogItemGap
                        y: modelData.warning ? Theme.responsibilityDialogWarnPaddingY : 0
                        width: parent.width - x
                        spacing: 2
                        Text {
                            width: parent.width
                            text: modelData.title
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.responsibilityDialogItemTitleSize
                            font.weight: Font.Medium
                            color: Theme.fg
                            renderType: Text.NativeRendering
                        }
                        Text {
                            width: parent.width
                            text: modelData.text
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.responsibilityDialogItemTextSize
                            lineHeightMode: Text.FixedHeight
                            lineHeight: Theme.responsibilityDialogItemTextSize * 1.5
                            color: Theme.fgSecondary
                            renderType: Text.NativeRendering
                        }
                    }
                }
            }
            Text {
                width: parent.width
                text: root.bridge ? root.bridge.responsibilityParagraph : root.txt("d1Personal", "На рабочем компьютере действуют правила вашей организации о записи совещаний.")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.responsibilityDialogItemTextSize
                lineHeightMode: Text.FixedHeight
                lineHeight: Theme.responsibilityDialogItemTextSize * 1.5
                color: Theme.fgSecondary
                renderType: Text.NativeRendering
            }
            NoteBanner {
                visible: root.bridge ? root.bridge.flyWarning !== "" : false
                variant: "warn"
                iconName: "alert"
                body: root.bridge ? root.bridge.flyWarning : ""
            }
            Item {
                width: parent.width
                visible: root.bridge ? root.bridge.detectorVisible : false
                height: visible ? checkRow.implicitHeight + Theme.responsibilityDialogCheckPaddingTop : 0
                Rectangle { width: parent.width; height: 1; color: Theme.borderSoft }
                RowLayout {
                    id: checkRow
                    width: parent.width
                    y: Theme.responsibilityDialogCheckPaddingTop
                    spacing: Theme.checkboxLabelGap
                    CheckBox {
                        id: detector
                        objectName: "meetingResponsibilityDetector"
                        Layout.alignment: Qt.AlignTop
                        implicitWidth: Theme.checkboxSize
                        implicitHeight: Theme.checkboxSize
                        padding: 0
                        enabled: root.bridge ? root.bridge.detectorEnabled : true
                        checked: root.offerChecked
                        onToggled: root.offerChecked = checked
                        onActiveFocusChanged: if (activeFocus) scroller.contentY = Math.max(0, scroller.contentHeight - scroller.height)
                        Accessible.name: root.txt("offer", "Предлагать запись, когда начинается звонок")
                        KeyNavigation.tab: cancel
                        KeyNavigation.backtab: accept
                        Keys.onEscapePressed: root.reject()
                        indicator: Rectangle {
                            width: Theme.checkboxSize
                            height: Theme.checkboxSize
                            radius: Theme.checkboxRadius
                            color: !detector.enabled ? Theme.checkboxDisabledBg : detector.checked ? Theme.checkboxOnBg : Theme.checkboxBg
                            border.width: Theme.checkboxBorder
                            border.color: detector.checked ? Theme.checkboxOnBg : Theme.checkboxBorderColor
                            Icon { anchors.centerIn: parent; name: "check"; size: Theme.checkboxIcon; color: Theme.checkboxOnFg; visible: detector.checked }
                            Rectangle { anchors.fill: parent; anchors.margins: -(Theme.focusOffset + Theme.focusWidth); radius: Theme.focusRadius; color: "transparent"; border.width: Theme.focusWidth; border.color: Theme.stateFocusRing; visible: detector.visualFocus }
                        }
                        contentItem: Item {}
                    }
                    Column {
                        Layout.fillWidth: true
                        Text {
                            width: parent.width
                            text: root.txt("offer", "Предлагать запись, когда начинается звонок")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: detector.enabled ? Theme.fg : Theme.fgDisabled
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.responsibilityDialogCheckLabelSize
                            renderType: Text.NativeRendering
                        }
                        Text {
                            width: parent.width
                            text: root.bridge ? root.bridge.detectorCaption : root.txt("d1OfferCaption", "Программа только спросит — запись сама не начнётся")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: detector.enabled ? Theme.fgMuted : Theme.fgDisabled
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.responsibilityDialogCheckSubSize
                            renderType: Text.NativeRendering
                        }
                    }
                }
            }
        }
    }
    footer: Item {
        implicitHeight: Theme.dialogFooterPaddingTop + (saveError.visible ? saveError.implicitHeight + Theme.dialogFooterGap : 0) + footerRow.implicitHeight + Theme.dialogFooterPaddingBottom
        NoteBanner {
            id: saveError
            objectName: "meetingResponsibilitySaveError"
            x: Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop
            width: parent.width - x * 2
            visible: root.bridge && root.bridge.saveError !== undefined ? root.bridge.saveError !== "" : false
            variant: "error"
            iconName: "alert"
            body: root.bridge && root.bridge.saveError !== undefined ? root.bridge.saveError : ""
        }
        Rectangle { width: parent.width; height: 1; color: Theme.borderSoft }
        RowLayout {
            id: footerRow
            x: Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop + (saveError.visible ? saveError.implicitHeight + Theme.dialogFooterGap : 0)
            width: parent.width - Theme.dialogFooterPaddingX * 2
            spacing: Theme.dialogFooterGap
            Item { Layout.fillWidth: true }
            AvButton {
                id: cancel
                objectName: "meetingResponsibilityCancel"
                text: root.txt("cancel", "Отмена")
                KeyNavigation.tab: accept
                KeyNavigation.backtab: detector.visible && detector.enabled ? detector : accept
                Keys.onReturnPressed: root.reject()
                Keys.onEnterPressed: root.reject()
                Keys.onEscapePressed: root.reject()
                onClicked: root.reject()
            }
            AvButton {
                id: accept
                objectName: "meetingResponsibilityAccept"
                text: root.txt("accept", "Понятно, включить")
                variant: "primary"
                KeyNavigation.tab: detector.visible && detector.enabled ? detector : cancel
                KeyNavigation.backtab: cancel
                Keys.onReturnPressed: root.accept()
                Keys.onEnterPressed: root.accept()
                Keys.onEscapePressed: root.reject()
                onClicked: root.accept()
            }
        }
    }
}
