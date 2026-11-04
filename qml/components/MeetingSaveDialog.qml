// D4 «Сохранить как — <название>» — design/meetings/spec.md §3.8/§4.7, tokens.md §4,
// макет 07-templates.html. M4: только шаблон «Стенограмма» и формат .md/.txt; остальные
// шаблоны — M5, группа «С ИИ» — M9 (не показывается). Файл пишет Python (bridge.saveAs):
// системный диалог файлов, 0600, санитизированное имя, перезапись — только с подтверждением.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

AvDialog {
    id: root
    objectName: "meetingSaveDialog"

    property var bridge: null
    property int formatIndex: 0
    readonly property var texts: bridge && bridge.cardTexts !== undefined ? bridge.cardTexts : ({})
    readonly property var card: bridge && bridge.card !== undefined ? bridge.card : ({})
    readonly property var formats: [".md", ".txt"]
    readonly property string error: bridge && bridge.cardSaveError !== undefined ? bridge.cardSaveError : ""
    property var previewRows: []

    function txt(key) { return texts[key] !== undefined ? texts[key] : "" }
    function saveResult(result) {
        if (result === "exists") root.replaceDialog.open()
        else if (result !== "saved" && result !== "pending") root.open()
    }
    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onCardSaveFinished(result) {
            if (result !== "stale" && root.bridge && root.bridge.cardOpen !== false)
                root.saveResult(result)
        }
    }
    function refreshPreview() {
        var model = bridge ? bridge.cardRows : null
        var rows = []
        var count = model ? Math.min(6, model.count) : 0 // макет 07: шесть первых строк.
        for (var i = 0; i < count; ++i) rows.push(model.get(i))
        previewRows = rows
    }

    parent: Overlay.overlay
    brandHeader: true
    heading: txt("saveTitle") + (card.title !== undefined ? card.title : "")
    note: txt("saveNote")
    cancelText: txt("saveCancel")
    confirmText: txt("saveConfirm")
    width: parent ? Math.min(Theme.meetingDialogSaveW, parent.width - 24) : Theme.meetingDialogSaveW

    onAboutToShow: refreshPreview()
    // Esc/«Отмена» возвращают фокус на «Сохранить как…» (spec §7).
    onClosed: if (returnFocusItem && returnFocusItem.visible) returnFocusItem.forceActiveFocus(Qt.TabFocusReason)
    onCancelled: if (bridge) bridge.clearCardSaveError()
    // Сначала закрывается D4, потом открывается системный диалог; отмена или ошибка
    // возвращают D4 с выбранным форматом (и строкой ошибки).
    onConfirmed: Qt.callLater(function() {
        if (!root.bridge) return
        var result = root.bridge.saveAs(root.formatIndex === 0 ? "md" : "txt")
        root.saveResult(result)
    })

    // Файл по выбранному пути уже есть (или появился после системного диалога):
    // заменяется только по «Заменить»; фокус на «Отмена», «Отмена»/Esc — обратно в D4.
    readonly property AvDialog replaceDialog: AvDialog {
        objectName: "meetingReplaceDialog"
        parent: root.parent // Overlay.overlay окна D4
        brandHeader: true
        heading: root.heading
        iconName: "alert"
        message: root.txt("replacePrefix")
            + (root.bridge && root.bridge.cardReplaceFile !== undefined ? root.bridge.cardReplaceFile : "")
            + root.txt("replaceSuffix")
        cancelText: root.txt("replaceCancel")
        confirmText: root.txt("replaceConfirm")
        safeConfirmation: true
        onCancelled: {
            if (root.bridge) root.bridge.cancelReplace()
            root.open()
        }
        onConfirmed: Qt.callLater(function() {
            if (root.bridge) root.saveResult(root.bridge.replaceSaveTarget())
        })
    }

    // Низ D4: подпись о копиях и правах + «Отмена» / «Выбрать папку и сохранить…»,
    // рамка сверху borderSoft (tokens.md §4). Подпись в две строки — своя раскладка без
    // RowLayout (иначе у переносимого текста цикл implicitHeight).
    footer: Item {
        implicitHeight: Theme.dialogFooterPaddingTop + Math.max(footNote.height, saveConfirm.height)
            + Theme.dialogFooterPaddingBottom
        Rectangle { width: parent.width; height: Theme.borderHairline; color: Theme.borderSoft }
        Text {
            id: footNote
            objectName: "meetingSaveNote"
            x: Theme.dialogFooterPaddingX
            y: Theme.dialogFooterPaddingTop + Math.max(0, (saveConfirm.height - height) / 2)
            width: Math.max(0, saveCancel.x - Theme.dialogFooterGap - x)
            text: root.note
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.meetingDialogSaveFootSize
            renderType: Text.NativeRendering
        }
        AvButton {
            id: saveCancel
            objectName: "meetingSaveCancel"
            x: saveConfirm.x - Theme.dialogFooterGap - width
            y: Theme.dialogFooterPaddingTop + Math.max(0, (footNote.height - height) / 2)
            text: root.cancelText
            onClicked: root.reject()
        }
        AvButton {
            id: saveConfirm
            objectName: "meetingSaveConfirm"
            x: parent.width - Theme.dialogFooterPaddingX - width
            y: saveCancel.y
            text: root.confirmText
            iconName: "save"
            variant: "primary"
            onClicked: root.accept()
        }
    }

    contentItem: Item {
        objectName: "dialogBody"
        implicitHeight: Math.max(list.height, pane.fullHeight)
        Accessible.role: Accessible.Dialog
        Accessible.name: root.heading

        Rectangle {
            id: list
            width: parent.width < 700 ? 190 : Theme.meetingDialogSaveListW // 07: 190 при окне < 700.
            height: Math.max(pane.fullHeight, item.height + 20)
            color: "transparent"
            Rectangle { x: parent.width - width; width: Theme.borderHairline; height: parent.height; color: Theme.border }
            Rectangle {
                id: item
                objectName: "meetingSaveTemplate"
                x: 8
                y: 10 // 07: список — паддинг 10 8.
                width: parent.width - 16 - Theme.borderHairline
                height: itemColumn.height + Theme.meetingDialogSaveItemPaddingY * 2
                radius: Theme.meetingDialogSaveItemRadius
                color: Theme.primaryBg
                Icon {
                    x: Theme.meetingDialogSaveItemPaddingX
                    y: Theme.meetingDialogSaveItemPaddingY + 2
                    name: "doc"
                    size: 16
                    color: Theme.primary
                }
                Column {
                    id: itemColumn
                    x: Theme.meetingDialogSaveItemPaddingX + 16 + 10
                    y: Theme.meetingDialogSaveItemPaddingY
                    width: parent.width - x - Theme.meetingDialogSaveItemPaddingX
                    Text {
                        width: parent.width
                        text: root.txt("templateTitle")
                        textFormat: Text.PlainText
                        color: Theme.fg
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingDialogSaveItemTitleSize
                        font.weight: Font.Medium
                        renderType: Text.NativeRendering
                    }
                    Text {
                        width: parent.width
                        text: root.txt("templateCaption")
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        color: Theme.fgMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingDialogSaveItemDescSize
                        lineHeight: Theme.meetingDialogSaveItemDescSize * 1.35
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                    }
                }
            }
        }

        Column {
            id: pane
            x: list.width + Theme.meetingDialogSavePanePaddingX
            y: Theme.meetingDialogSavePanePaddingY
            width: parent.width - x - Theme.meetingDialogSavePanePaddingX
            spacing: Theme.meetingDialogSavePaneGap
            readonly property real fullHeight: height + Theme.meetingDialogSavePanePaddingY * 2

            RowLayout {
                width: parent.width
                spacing: 8
                Text {
                    Layout.fillWidth: true
                    text: root.txt("templateTitle")
                    textFormat: Text.PlainText
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontH3SubsectionSize
                    font.weight: Font.Medium
                    renderType: Text.NativeRendering
                }
                AvSegmented {
                    id: format
                    objectName: "meetingSaveFormat"
                    options: root.formats
                    currentIndex: root.formatIndex
                    onCurrentIndexChanged: root.formatIndex = currentIndex
                }
            }

            Rectangle {
                width: parent.width
                height: previewColumn.height + Theme.cardBorder * 2
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: Theme.border
                Column {
                    id: previewColumn
                    x: Theme.cardBorder
                    y: Theme.cardBorder
                    width: parent.width - Theme.cardBorder * 2
                    Repeater {
                        model: root.previewRows
                        MeetingTranscriptRow {
                            width: previewColumn.width
                            first: index === 0
                            kind: modelData.kind
                            time: modelData.time
                            who: modelData.who
                            whoKind: modelData.whoKind
                            text: modelData.text
                            iconName: modelData.icon
                            accessibleName: modelData.accessibleName
                        }
                    }
                }
            }

            NoteBanner {
                objectName: "meetingSaveError"
                visible: root.error !== ""
                height: visible ? implicitHeight : 0
                variant: "error"
                iconName: "alert"
                body: root.error
            }
        }
    }
}
