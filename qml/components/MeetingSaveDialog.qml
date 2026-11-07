// D4 «Сохранить как — <название>» — design/meetings/spec.md §3.8/§4.7, tokens.md §4,
// макет 07-templates.html. Четыре локальных шаблона; группа «С ИИ» — M9.
// Предпросмотр и файл рендерит Python (bridge.savePreview / bridge.saveAs):
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
    property var preview: ({ "kind": "", "rows": [] })
    readonly property var templates: bridge && bridge.saveTemplates !== undefined ? bridge.saveTemplates : []
    readonly property int templateIndex: {
        if (!bridge || bridge.saveTemplate === undefined) return 0
        for (var i = 0; i < templates.length; ++i)
            if (templates[i].id === bridge.saveTemplate) return i
        return 0
    }
    function selectTemplate(index, reason) {
        if (!bridge || bridge.saveTemplate === undefined || index < 0 || index >= templates.length) return
        var target = templateItems.itemAt(index)
        if (!target) return
        var focusReason = reason === undefined ? Qt.MouseFocusReason : reason
        root.focusWithReason(target, focusReason)
        bridge.saveTemplate = templates[index].id
    }

    function txt(key) { return texts[key] !== undefined ? texts[key] : "" }
    function saveResult(result) {
        if (result === "exists") root.replaceDialog.open()
        else if (result !== "saved" && result !== "pending") root.open()
    }
    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onSavePreviewChanged() { if (root.visible) root.refreshPreview() }
        function onCardSaveFinished(result) {
            if (result !== "stale" && root.bridge && root.bridge.cardOpen !== false)
                root.saveResult(result)
        }
    }
    function refreshPreview() {
        preview = bridge && typeof bridge.savePreview === "function"
            ? bridge.savePreview() : ({ "kind": "", "rows": [] })
    }

    parent: Overlay.overlay
    brandHeader: true
    heading: txt("saveTitle") + (card.title !== undefined ? card.title : "")
    note: txt("saveNote")
    cancelText: txt("saveCancel")
    confirmText: txt("saveConfirm")
    width: parent ? Math.min(Theme.meetingDialogSaveW, parent.width - 24) : Theme.meetingDialogSaveW

    onAboutToShow: refreshPreview()
    onOpened: if (templates.length) selectTemplate(templateIndex, Qt.PopupFocusReason)
    // Esc/«Отмена» возвращают фокус на «Сохранить как…» (spec §7).
    onClosed: if (returnFocusItem && returnFocusItem.visible) root.focusWithReason(returnFocusItem, Qt.TabFocusReason)
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
            lineHeight: Theme.meetingDialogSaveFootSize * 1.5
            lineHeightMode: Text.FixedHeight
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
            height: Math.max(pane.fullHeight, templateColumn.height + 20)
            color: "transparent"
            Rectangle { x: parent.width - width; width: Theme.borderHairline; height: parent.height; color: Theme.border }
            Column {
                id: templateColumn
                x: 8
                y: 10
                width: parent.width - 16 - Theme.borderHairline
                Repeater {
                    id: templateItems
                    model: root.templates
                    Button {
                        id: option
                        objectName: "meetingSaveTemplate"
                        readonly property string iconName: modelData.icon
                        readonly property bool selected: index === root.templateIndex
                        width: templateColumn.width
                        height: itemColumn.height + Theme.meetingDialogSaveItemPaddingY * 2
                        padding: 0
                        // Keep the active item eligible until focus has actually left it.
                        activeFocusOnTab: selected || activeFocus
                        Accessible.name: modelData.title + " · " + modelData.caption
                        Accessible.role: Accessible.RadioButton
                        Accessible.checkable: true
                        Accessible.checked: selected
                        onClicked: root.selectTemplate(index)
                        Keys.onPressed: {
                            switch (event.key) {
                            case Qt.Key_Down: root.selectTemplate(Math.min(index + 1, root.templates.length - 1), Qt.TabFocusReason); break
                            case Qt.Key_Up: root.selectTemplate(Math.max(index - 1, 0), Qt.TabFocusReason); break
                            case Qt.Key_Home: root.selectTemplate(0, Qt.TabFocusReason); break
                            case Qt.Key_End: root.selectTemplate(root.templates.length - 1, Qt.TabFocusReason); break
                            case Qt.Key_Return:
                            case Qt.Key_Enter: break // выбор шаблона не сохраняет файл
                            default: return
                            }
                            event.accepted = true
                        }
                        background: Rectangle {
                            radius: Theme.meetingDialogSaveItemRadius
                            color: option.selected ? Theme.primaryBg : "transparent"
                            Rectangle {
                                objectName: "meetingSaveTemplateFocus"
                                anchors.fill: parent
                                anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                                radius: Theme.focusRadius
                                color: "transparent"
                                border.width: Theme.focusWidth
                                border.color: Theme.stateFocusRing
                                visible: option.visualFocus
                                antialiasing: true
                            }
                        }
                        contentItem: Item {
                            Icon {
                                objectName: "meetingSaveTemplateIcon"
                                x: Theme.meetingDialogSaveItemPaddingX
                                y: Theme.meetingDialogSaveItemPaddingY + 2
                                name: option.iconName
                                strokeWidth: option.iconName === "chart" ? 1.8 : 1.5
                                size: 16
                                color: option.selected ? Theme.primary : Theme.fgMuted
                            }
                            Column {
                                id: itemColumn
                                x: Theme.meetingDialogSaveItemPaddingX + 16 + 10
                                y: Theme.meetingDialogSaveItemPaddingY
                                width: parent.width - x - Theme.meetingDialogSaveItemPaddingX
                                Text {
                                    objectName: "meetingSaveTemplateTitle"
                                    width: parent.width
                                    text: modelData.title
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WordWrap
                                    color: Theme.fg
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.meetingDialogSaveItemTitleSize
                                    font.weight: option.selected ? Font.Medium : Font.Normal
                                    lineHeight: Theme.meetingDialogSaveItemTitleSize * 1.5
                                    lineHeightMode: Text.FixedHeight
                                    renderType: Text.NativeRendering
                                }
                                Text {
                                    width: parent.width
                                    text: modelData.caption
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
                    text: root.templates.length ? root.templates[root.templateIndex].title : ""
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
                id: previewCard
                objectName: "meetingSavePreview"
                readonly property bool transcript: root.preview.kind === "transcript"
                readonly property real insetX: transcript ? Theme.cardBorder : root.preview.kind === "stats" ? 16 : 12
                readonly property real insetY: transcript ? Theme.cardBorder : root.preview.kind === "stats" ? 14 : 12
                width: parent.width
                height: Math.min(440, previewColumn.height + insetY * 2)
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: Theme.border
                Flickable {
                    anchors.fill: parent
                    anchors.leftMargin: previewCard.insetX
                    anchors.rightMargin: previewCard.insetX
                    anchors.topMargin: previewCard.insetY
                    anchors.bottomMargin: previewCard.insetY
                    contentWidth: width
                    contentHeight: previewColumn.height
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { }
                    Column {
                        id: previewColumn
                        width: parent.width
                        spacing: root.preview.kind === "text_only" ? 21 : 0
                        Repeater {
                            model: root.preview.kind === "transcript" ? root.preview.rows : []
                            MeetingTranscriptRow {
                                objectName: "meetingSavePreviewRow"
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
                        Repeater {
                            model: root.preview.kind === "text_only" ? root.preview.rows : []
                            Text {
                                objectName: "meetingSaveParagraph"
                                width: previewColumn.width
                                text: (modelData.kind === "mark" ? "— " : "") + modelData.text
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                font.family: Theme.fontUi
                                font.pixelSize: 13
                                lineHeight: font.pixelSize * 1.6
                                lineHeightMode: Text.FixedHeight
                                color: modelData.kind === "mark" ? Theme.fgMuted : Theme.fg
                                renderType: Text.NativeRendering
                            }
                        }
                        Text {
                            objectName: "meetingSaveContinuation"
                            visible: root.preview.kind === "text_only" && text !== ""
                            width: previewColumn.width
                            text: root.preview.kind === "text_only" ? root.preview.moreText : ""
                            textFormat: Text.PlainText
                            font.family: Theme.fontUi
                            font.pixelSize: 13
                            lineHeight: font.pixelSize * 1.6
                            lineHeightMode: Text.FixedHeight
                            color: Theme.fgMuted
                            renderType: Text.NativeRendering
                        }
                        Column {
                            width: parent.width
                            visible: root.preview.kind === "stats"
                            height: visible ? implicitHeight : 0
                            spacing: 9 // _p4.py stats(): padding 14/16, gap 9.
                            Flow {
                                width: parent.width
                                height: childrenRect.height
                                Repeater {
                                    model: root.preview.kind === "stats" ? root.preview.summary : []
                                    Text {
                                        text: modelData.text
                                        textFormat: Text.PlainText
                                        font.family: modelData.mono ? Theme.fontMono : Theme.fontUi
                                        font.pixelSize: 13
                                        lineHeight: 13 * 1.5
                                        lineHeightMode: Text.FixedHeight
                                        color: Theme.fgMuted
                                        renderType: Text.NativeRendering
                                    }
                                }
                            }
                            Repeater {
                                model: root.preview.kind === "stats" ? root.preview.rows : []
                                RowLayout {
                                    id: statRow
                                    objectName: "meetingSaveStatRow"
                                    width: previewColumn.width
                                    spacing: 10
                                    Text {
                                        objectName: "meetingSaveStatLabel"
                                        Layout.fillWidth: true
                                        Layout.preferredWidth: Math.max(0, (statRow.width - 122) / 2)
                                        Layout.minimumWidth: 0
                                        text: modelData.who
                                        textFormat: Text.PlainText
                                        elide: Text.ElideRight
                                        font.family: Theme.fontUi
                                        font.pixelSize: 13
                                        lineHeight: 13 * 1.5
                                        lineHeightMode: Text.FixedHeight
                                        color: Theme.fg
                                        renderType: Text.NativeRendering
                                    }
                                    Rectangle {
                                        id: statTrack
                                        objectName: "meetingSaveStatBar"
                                        visible: root.preview.hasShares
                                        Layout.fillWidth: true
                                        Layout.preferredWidth: Math.max(0, (statRow.width - 122) / 2)
                                        Layout.minimumWidth: 40
                                        height: 8
                                        radius: 3
                                        color: Theme.bgSurface2
                                        Item {
                                            objectName: "meetingSaveStatFillClip"
                                            width: parent.width * (modelData.share || 0) / 100
                                            height: parent.height
                                            clip: true
                                            // Clip a full rounded track to the share: straight right edge.
                                            Rectangle {
                                                width: statTrack.width
                                                height: statTrack.height
                                                radius: statTrack.radius
                                                color: Theme.primary
                                            }
                                        }
                                    }
                                    Text {
                                        Layout.preferredWidth: 52
                                        text: modelData.minutes
                                        textFormat: Text.PlainText
                                        font.family: Theme.fontMono
                                        font.pixelSize: 12
                                        lineHeight: 13 * 1.5
                                        lineHeightMode: Text.FixedHeight
                                        color: Theme.fgMuted
                                        renderType: Text.NativeRendering
                                    }
                                    Text {
                                        objectName: "meetingSaveStatPercent"
                                        visible: root.preview.hasShares
                                        Layout.preferredWidth: 40
                                        text: modelData.percent
                                        textFormat: Text.PlainText
                                        font.family: Theme.fontMono
                                        font.pixelSize: 12
                                        lineHeight: 13 * 1.5
                                        lineHeightMode: Text.FixedHeight
                                        color: Theme.fg
                                        renderType: Text.NativeRendering
                                    }
                                }
                            }
                            Text {
                                visible: text !== ""
                                text: root.preview.kind === "stats" ? root.preview.moreText : ""
                                textFormat: Text.PlainText
                                font.family: Theme.fontUi
                                font.pixelSize: 13
                                lineHeight: 13 * 1.5
                                lineHeightMode: Text.FixedHeight
                                color: Theme.fgMuted
                                renderType: Text.NativeRendering
                            }
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
