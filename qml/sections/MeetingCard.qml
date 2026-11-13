// Карточка встречи A «Документ» — design/meetings/spec.md §3.7/§4.7/§5.2, §7;
// значения — design/meetings/tokens.md §3. Макеты 05-meeting-A.html, 06-meeting-states.html.
// M7: имена, объединение, пометки; плеер — Could.
// Вся карточка живёт в общем Flickable окна (§5.2): своей прокрутки нет.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

FocusScope {
    id: root
    objectName: "meetingCard"

    property var bridge: null
    readonly property var card: bridge && bridge.card !== undefined && bridge.card.open === true ? bridge.card : ({})
    readonly property var redo: card.diarizationRedo || ({})
    readonly property var diarInstall: bridge && bridge.diarizationInstall !== undefined ? bridge.diarizationInstall : ({})
    readonly property var texts: bridge && bridge.cardTexts !== undefined ? bridge.cardTexts : ({})
    readonly property var rowsModel: bridge && bridge.cardRows !== undefined ? bridge.cardRows : null
    readonly property string view: card.view !== undefined ? card.view : ""
    // Точка 760 по ширине контента (spec §5.2): шире — панель «Участники» справа.
    readonly property bool hasParticipants: card.diarizationInstallVisible !== true && (card.participants || []).length > 0
    readonly property bool wide: width >= Theme.sizeMeetingSplitContentW
    readonly property bool panelState: card.panel !== undefined && card.panel.title !== undefined
    readonly property real tableWidth: wide && hasParticipants ? width - Theme.participantsPanelGap - Theme.participantsPanelW : width
    // Рамка карточек: в тёмной теме border полупрозрачный; Qt не кладёт под рамку заливку,
    // поэтому смешиваем его с bgSurface заранее (CSS border-box), как OverlaySurface (M3).
    readonly property color cardBorderColor: Qt.tint(Theme.bgSurface, Theme.border)
    // Сколько строк таблицы создано: инкрементальная модель не пересоздаёт старые.
    readonly property Item moreFocusItem: moreButton
    property int rowsCreated: 0
    property int currentRow: 0

    onVisibleChanged: {
        if (!visible) closeOverlays()
    }
    onCardChanged: {
        redoDialog.invalidate()
        if (card.open !== true) closeOverlays()
        else if (card.namesEditable !== true && mergePopover) mergePopover.close()
    }
    function closeOverlays() {
        if (moreMenu) moreMenu.close()
        if (redoDialog) redoDialog.close()
        if (namesDrawer) namesDrawer.close()
        if (mergePopover) mergePopover.close()
        if (saveDialog) saveDialog.close()
        if (speakerToast) speakerToast.visible = false
    }

    function requestRedo(origin) {
        if (!bridge || redo.menuEnabled !== true) return
        if (redo.needsWarning) redoDialog.showWarning(redo, origin)
        else if (bridge.rediarizeMeeting(redo.meetingId, redo.expectedGeneration, false))
            Qt.callLater(focusRedo)
    }
    function focusRedo() {
        if (redoAction.visible) redoAction.forceActiveFocus(Qt.TabFocusReason)
        else table.forceActiveFocus(Qt.TabFocusReason)
    }

    signal backRequested()

    width: parent ? parent.width : Theme.sizeContentColMax
    implicitHeight: column.implicitHeight

    function inlineLineTop(labelHeight, controlsHeight) {
        return (Math.max(labelHeight, controlsHeight) - labelHeight) / 2
    }
    function inlineHalfLeading(lineHeight, fontHeight) {
        return Math.max(0, (lineHeight - fontHeight) / 2)
    }
    function txt(key) { return texts[key] !== undefined ? texts[key] : "" }
    function focusBack(reason) { back.forceActiveFocus(reason === undefined ? Qt.TabFocusReason : reason) }
    function openSaveDialog() { if (root.card.showActions === true) saveDialog.open() }
    function requestRename(origin) {
        if (!bridge || renameDialog.pending) return
        renameDialog.returnFocusItem = origin
        bridge.cardAction("rename")
    }
    readonly property Item overlayViewport: {
        var item = root.parent
        while (item) {
            if (item.contentY !== undefined && item.contentHeight !== undefined) return item
            if (!item.parent) break
            item = item.parent
        }
        return root.parent
    }
    property var namesOrigin: null
    function openNames(origin) {
        namesOrigin = origin
        if (wide) participants.focusFirst()
        else namesDrawer.open()
    }
    function openMerge(person, origin, panel) {
        mergePopover.source = person
        mergePopover.ownerPanel = panel
        panel.highlightedSpeaker = person.id
        mergePopover.returnFocusItem = origin
        var point = panel.speakerAnchor(person.id, mergePopover.parent)
        mergePopover.x = Math.max(0, point.x - Theme.mergePopoverW - Theme.participantsPanelGap + Theme.focusOffset + Theme.focusWidth)
        mergePopover.anchorCenterY = point.y + point.height / 2
        mergePopover.open()
    }

    function moveRow(step) {
        var count = rows.count
        if (count === 0) return
        root.currentRow = Math.max(0, Math.min(count - 1, root.currentRow + step))
        var item = rows.itemAt(root.currentRow)
        if (item) item.forceActiveFocus(Qt.TabFocusReason)
    }

    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onRenameRequested(meetingId, title) {
            if (root.card.id !== meetingId || renameDialog.pending) return
            renameDialog.meetingId = meetingId
            renameDialog.meetingTitle = title
            Qt.callLater(function() { renameDialog.open() })
        }
        function onCardSaveRequested() { Qt.callLater(root.openSaveDialog) }
    }

    // Строки таблицы создаются порциями (spec §5.2 — одна прокрутка окна, без своей):
    // открытие 9000 реплик не держит GUI-поток дольше одной порции; живые реплики
    // по одной добавляются сразу, старые делегаты не пересоздаются.
    // Сброс модели тоже идёт порциями: старые строки убираются с конца (удаление
    // тысяч делегатов разом держит поток секундами), а пока убираются — таблица
    // схлопнута и обрезана (не visible/opacity: те обходят каждый делегат).
    readonly property int rowsPerFrame: 100
    readonly property int rowsDrainPerFrame: 300
    property int rowsFed: 0
    property int rowsStale: 0
    property bool rowsDraining: false
    function feedRows() {
        if (root.rowsStale > 0) {
            var k = Math.min(root.rowsStale, root.rowsDrainPerFrame)
            shownRows.remove(shownRows.count - k, k)
            root.rowsStale -= k
            rowFeeder.restart()
            return
        }
        root.rowsDraining = false
        var source = root.rowsModel
        var total = source ? source.count : 0
        var end = Math.min(total, root.rowsFed + root.rowsPerFrame)
        var batch = []
        for (var i = root.rowsFed; i < end; ++i) batch.push(source.get(i))
        root.rowsFed = end
        if (batch.length > 0) shownRows.append(batch)
        if (end < total) rowFeeder.restart()
        else rowFeeder.stop()
    }
    function refeedRows() {
        rowFeeder.stop()
        root.rowsFed = 0
        if (shownRows.count > root.rowsPerFrame) {
            root.rowsStale = shownRows.count
            root.rowsDraining = true
        } else {
            root.rowsStale = 0
            shownRows.clear()
        }
        root.feedRows()
    }
    onRowsModelChanged: refeedRows()
    Component.onCompleted: refeedRows()
    ListModel { id: shownRows }
    Timer { id: rowFeeder; interval: 0; onTriggered: root.feedRows() }
    Connections {
        target: root.rowsModel
        function onModelReset() { root.refeedRows() }
        function onRowsInserted() { if (!rowFeeder.running) root.feedRows() }
        function onDataChanged(first, last) {
            if (root.rowsDraining) return
            for (var i = first.row; i <= last.row && i < shownRows.count; ++i)
                shownRows.set(i, root.rowsModel.get(i))
        }
    }

    Column {
        id: column
        width: parent.width

        // ── «← Все встречи» ────────────────────────────────────────────────
        AbstractButton {
            id: back
            objectName: "meetingCardBack"
            activeFocusOnTab: true
            implicitWidth: backRow.implicitWidth
            implicitHeight: 19.5 // tokens.md §3.1: h 19.5 (13 × 1.5).
            Accessible.role: Accessible.Button
            Accessible.name: root.txt("back")
            onClicked: root.backRequested()
            Keys.onReturnPressed: root.backRequested()
            Keys.onEnterPressed: root.backRequested()
            contentItem: Row {
                id: backRow
                spacing: Theme.meetingHeadBackGap
                Icon {
                    anchors.verticalCenter: parent.verticalCenter
                    name: "chevl"
                    size: Theme.meetingHeadBackIcon
                    color: Theme.primary
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.txt("back")
                    textFormat: Text.PlainText
                    color: Theme.primary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.meetingHeadBackSize
                    // Строка 19.5 с глифами по центру (половинный интерлиньяж, как в CSS):
                    // FixedHeight кладёт глифы к верху строки — на 1 px выше макета.
                    height: Theme.meetingHeadBackSize * Theme.fontBodyLineHeight
                    verticalAlignment: Text.AlignVCenter
                    renderType: Text.NativeRendering
                }
            }
            background: Rectangle {
                color: "transparent"
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                    radius: Theme.focusRadius
                    color: "transparent"
                    border.width: Theme.focusWidth
                    border.color: Theme.stateFocusRing
                    visible: back.visualFocus
                }
            }
        }

        Item { width: 1; height: Theme.meetingHeadBackMarginBottom }

        // ── шапка: название, карандаш, бейдж; подстрока; действия справа ──────
        // Item, не RowLayout: раскладка округляет высоту 44.5 до 45, и всё ниже
        // съезжает на полпикселя (отметки на 1 px ниже макета).
        Item {
            id: head
            width: parent.width
            height: subtitle.wordWrap && subtitle.height > Theme.fontSmallSize * Theme.fontBodyLineHeight
                ? Math.floor(head.y + Math.max(headText.height, headActions.visible ? headActions.height : 0)) - head.y
                : Math.max(headText.height, headActions.visible ? headActions.height : 0)

            Column {
                id: headText
                // макет .shead: зазор 12 между колонкой названия и кнопками.
                width: Math.max(0, parent.width - (headActions.visible ? headActions.width + 12 : 0))

                Flow {
                    id: titleRow
                    width: parent.width
                    spacing: Theme.meetingHeadTitleGap
                    readonly property real lineH: Theme.fontH2SectionSize * Theme.fontH2SectionLineHeight

                    Text {
                        id: title
                        objectName: "meetingCardTitle"
                        width: Math.min(implicitWidth, titleRow.width)
                        text: root.card.title !== undefined ? root.card.title : ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Theme.fg
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontH2SectionSize
                        font.weight: Font.Bold
                        lineHeight: titleRow.lineH
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                    }
                    Item {
                        visible: root.card.showEdit === true
                        width: Theme.meetingHeadEditIcon
                        height: titleRow.lineH
                        AbstractButton {
                            id: editButton
                            objectName: "meetingCardEdit"
                            anchors.centerIn: parent
                            width: Theme.meetingHeadEditIcon
                            height: Theme.meetingHeadEditIcon
                            enabled: root.card.renameEnabled === true
                            focusPolicy: Qt.StrongFocus
                            onClicked: root.requestRename(editButton)
                            Keys.onReturnPressed: root.requestRename(editButton)
                            Keys.onEnterPressed: root.requestRename(editButton)
                            Accessible.name: root.txt("rename")
                            contentItem: Icon { name: "edit"; size: Theme.meetingHeadEditIcon; color: Theme.fgMuted }
                            background: Item {
                                Rectangle {
                                    anchors.fill: parent
                                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                                    radius: Theme.focusRadius
                                    color: "transparent"
                                    border.width: Theme.focusWidth
                                    border.color: Theme.stateFocusRing
                                    visible: editButton.visualFocus
                                }
                            }
                        }
                    }
                    Item {
                        width: badge.width
                        height: titleRow.lineH
                        MeetingBadge {
                            id: badge
                            objectName: "meetingCardBadge"
                            // По центру строки по расчётной высоте 21.25 (не по снапнутой 21), как в макете.
                            y: (parent.height - implicitHeight) / 2
                            text: root.card.badge !== undefined ? root.card.badge.text : ""
                            kind: root.card.badge !== undefined ? root.card.badge.kind : "neutral"
                            iconName: root.card.badge !== undefined ? root.card.badge.icon : ""
                        }
                    }
                }

                Flow {
                    id: subtitle
                    objectName: "meetingCardSubtitle"
                    width: parent.width
                    readonly property var parts: root.card.subtitle !== undefined ? root.card.subtitle : ({})
                    readonly property bool wordWrap: root.redo.visible === true && !root.wide
                    readonly property var words: {
                        var result = []
                        function add(value, mono) {
                            var leading = (value || "").match(/^\s+/)
                            var pieces = (value || "").match(/\S+\s*/g) || []
                            // Fragment separators always use the UI font, never the clock's mono font.
                            if (leading && pieces.length && !mono) pieces[0] = leading[0] + pieces[0]
                            else if (leading) result.push({text: leading[0], mono: false})
                            for (var i = 0; i < pieces.length; ++i) result.push({text: pieces[i], mono: mono})
                        }
                        add(parts.prefix, false)
                        add(parts.mono, true)
                        add(parts.suffix, false)
                        return result
                    }
                    Repeater {
                        model: subtitle.wordWrap ? subtitle.words : []
                        Text {
                            id: subtitleWord
                            // Keep the half-pixel phase of wrapped inline lines.
                            transform: Translate { y: subtitleWord.y % 1 }
                            text: modelData.text
                            textFormat: Text.PlainText
                            color: Theme.fgMuted
                            font.family: modelData.mono ? Theme.fontMono : Theme.fontUi
                            font.pixelSize: Theme.fontSmallSize
                            font.letterSpacing: modelData.mono ? Theme.fontSmallSize * Theme.fontMonoInlineTracking : 0
                            lineHeight: Theme.fontSmallSize * Theme.fontBodyLineHeight
                            lineHeightMode: Text.FixedHeight
                            renderType: Text.NativeRendering
                        }
                    }
                    Text {
                        visible: !subtitle.wordWrap
                        text: subtitle.parts.prefix !== undefined ? subtitle.parts.prefix : ""
                        textFormat: Text.PlainText
                        color: Theme.fgMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmallSize
                        lineHeight: Theme.fontSmallSize * Theme.fontBodyLineHeight
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                    }
                    Text {
                        visible: !subtitle.wordWrap
                        text: subtitle.parts.mono !== undefined ? subtitle.parts.mono : ""
                        textFormat: Text.PlainText
                        color: Theme.fgMuted
                        font.family: Theme.fontMono
                        font.pixelSize: Theme.fontSmallSize
                        lineHeight: Theme.fontSmallSize * Theme.fontBodyLineHeight
                        lineHeightMode: Text.FixedHeight
                        font.letterSpacing: Theme.fontSmallSize * Theme.fontMonoInlineTracking
                        renderType: Text.NativeRendering
                    }
                    Text {
                        visible: !subtitle.wordWrap
                        text: subtitle.parts.suffix !== undefined ? subtitle.parts.suffix : ""
                        textFormat: Text.PlainText
                        color: Theme.fgMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmallSize
                        lineHeight: Theme.fontSmallSize * Theme.fontBodyLineHeight
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                    }
                }
            }

            Row {
                id: headActions
                x: parent.width - width
                visible: root.card.showActions === true
                spacing: Theme.meetingHeadActionsGap
                AvButton {
                    id: saveButton
                    objectName: "meetingCardSave"
                    text: root.txt("save")
                    iconName: "save"
                    variant: "primary"
                    onClicked: saveDialog.open()
                }
                AvButton {
                    id: moreButton
                    objectName: "meetingCardMore"
                    KeyNavigation.tab: keepAction.visible ? keepAction : redoAction.visible ? redoAction : installButton.visible ? installButton : table
                    width: Theme.meetingHeadIconButton
                    height: Theme.meetingHeadIconButton
                    leftPadding: 0
                    rightPadding: 0
                    Accessible.name: "⋯"
                    // Квадратная icon-кнопка 34×34: глиф по центру (tokens.md §3.1).
                    contentItem: Item {
                        Icon {
                            anchors.centerIn: parent
                            name: "more"
                            fill: true
                            size: Theme.buttonIconSize
                            color: moreButton.fgColor
                        }
                    }
                    onClicked: moreMenu.open()
                    Menu {
                        id: moreMenu
                        objectName: "meetingCardMenu"
                        y: moreButton.height + 4
                        x: moreButton.width - width
                        width: Theme.popoverMinW
                        padding: Theme.menuPadding + 1
                        background: Item {
                            OverlaySurface {
                                anchors.fill: parent
                                surfaceWidth: parent.width; surfaceHeight: parent.height
                                surfaceRadius: Theme.menuRadius
                                surfaceColor: Theme.bgSurface
                                borderWidth: 1
                                borderColor: Qt.tint(Theme.bgSurface, Theme.border)
                                shadowSourceColor: Theme.bgSurface
                                shadowX: Theme.shadowMenuX; shadowY: Theme.shadowMenuY
                                shadowRadius: Theme.shadowMenuRadius; shadowColor: Theme.shadowMenuColor
                            }
                        }
                        // Esc закрывает меню, фокус — обратно на «⋯» (spec §5.2).
                        onClosed: moreButton.forceActiveFocus(Qt.TabFocusReason)
                        MenuItem {
                            id: renameItem
                            objectName: "meetingCardRenameAction"
                            implicitHeight: Theme.fontMenuItemSize * Theme.fontBodyLineHeight + Theme.menuItemPaddingY * 2
                            leftPadding: 9; rightPadding: 9
                            contentItem: Row {
                                transform: Translate { y: Math.ceil(renameItem.y) - renameItem.y }
                                spacing: 9
                                Icon { name: "edit"; size: 16; color: renameItem.enabled ? Theme.fg : Theme.fgDisabled; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    text: renameItem.text; textFormat: Text.PlainText
                                    font.family: Theme.fontUi; font.pixelSize: 13
                                    color: renameItem.enabled ? Theme.fg : Theme.fgDisabled
                                    anchors.verticalCenter: parent.verticalCenter
                                    renderType: Text.NativeRendering
                                }
                            }
                            background: Rectangle { color: renameItem.highlighted ? Theme.stateHoverOnSurface : "transparent"; radius: Theme.menuItemRadius }
                            text: root.txt("rename")
                            enabled: root.card.renameEnabled === true
                            onTriggered: root.requestRename(moreButton)
                        }
                        MenuItem {
                            id: namesItem
                            objectName: "meetingCardNamesAction"
                            implicitHeight: Theme.fontMenuItemSize * Theme.fontBodyLineHeight + Theme.menuItemPaddingY * 2
                            leftPadding: 9; rightPadding: 9
                            contentItem: Row {
                                transform: Translate { y: Math.ceil(namesItem.y) - namesItem.y }
                                spacing: 9
                                Icon { name: "people"; size: 16; color: namesItem.enabled ? Theme.fg : Theme.fgDisabled; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    text: namesItem.text; textFormat: Text.PlainText
                                    font.family: Theme.fontUi; font.pixelSize: 13
                                    color: namesItem.enabled ? Theme.fg : Theme.fgDisabled
                                    anchors.verticalCenter: parent.verticalCenter
                                    renderType: Text.NativeRendering
                                }
                            }
                            background: Rectangle { color: namesItem.highlighted ? Theme.stateHoverOnSurface : "transparent"; radius: Theme.menuItemRadius }
                            text: root.txt("names")
                            enabled: root.redo.busy !== true && root.card.live !== true && root.card.showTable === true && (root.card.participants || []).some(function(p) { return p.kind === "speaker" })
                            onTriggered: Qt.callLater(function() { root.openNames(moreButton) })
                        }
                        MenuItem {
                            id: redoItem
                            objectName: "meetingCardRedoMenu"
                            implicitHeight: Theme.fontMenuItemSize * Theme.fontBodyLineHeight + Theme.menuItemPaddingY * 2
                            leftPadding: 9; rightPadding: 9
                            contentItem: Row {
                                transform: Translate { y: Math.ceil(redoItem.y) - redoItem.y }
                                spacing: 9
                                Icon { name: "refresh"; size: 16; color: redoItem.enabled ? Theme.fg : Theme.fgDisabled; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    text: redoItem.text; textFormat: Text.PlainText
                                    font.family: Theme.fontUi; font.pixelSize: 13
                                    color: redoItem.enabled ? Theme.fg : Theme.fgDisabled
                                    anchors.verticalCenter: parent.verticalCenter
                                    renderType: Text.NativeRendering
                                }
                            }
                            background: Rectangle { color: redoItem.highlighted ? Theme.stateHoverOnSurface : "transparent"; radius: Theme.menuItemRadius }
                            visible: root.redo.menuVisible === true
                            height: visible ? implicitHeight : 0
                            enabled: root.redo.menuEnabled === true
                            text: root.redo.menuText || ""
                            Accessible.name: text
                            onTriggered: Qt.callLater(function() { root.requestRedo(moreButton) })
                        }
                        MenuSeparator {
                            id: redoSeparator
                            objectName: "meetingRedoMenuSeparator"
                            implicitHeight: 9
                            leftPadding: 6; rightPadding: 6
                            topPadding: 4; bottomPadding: 4
                            contentItem: Rectangle {
                                implicitHeight: 1; color: Theme.borderSoft
                                transform: Translate { y: Math.ceil(redoSeparator.y) - redoSeparator.y }
                            }
                        }
                        // D2 «Удалить встречу» — M5.
                        MenuItem {
                            id: deleteItem
                            objectName: "meetingCardDeleteAction"
                            implicitHeight: Theme.fontMenuItemSize * Theme.fontBodyLineHeight + Theme.menuItemPaddingY * 2
                            leftPadding: 9; rightPadding: 9
                            contentItem: Row {
                                transform: Translate { y: Math.ceil(deleteItem.y) - deleteItem.y }
                                spacing: 9
                                Icon { name: "trash"; size: 16; color: Theme.dangerInk; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    text: deleteItem.text; textFormat: Text.PlainText
                                    font.family: Theme.fontUi; font.pixelSize: 13; color: Theme.dangerInk
                                    anchors.verticalCenter: parent.verticalCenter
                                    renderType: Text.NativeRendering
                                }
                            }
                            background: Rectangle { color: deleteItem.highlighted ? Theme.stateHoverOnSurface : "transparent"; radius: Theme.menuItemRadius }
                            text: root.txt("delete")
                            enabled: root.card.deleteEnabled === true
                            onTriggered: if (root.bridge) root.bridge.cardAction("delete")
                        }
                    }
                }
            }
        }

        // ── отметки (бейджи) ────────────────────────────────────────────────
        Item {
            id: marksBox
            width: parent.width
            readonly property var marks: root.card.marks !== undefined ? root.card.marks : []
            visible: marks.length > 0
            height: visible ? Theme.meetingHeadMarksMarginTop + marksFlow.height + Theme.meetingHeadMarksMarginBottom : 0
            Flow {
                id: marksFlow
                objectName: "meetingCardMarks"
                y: Theme.meetingHeadMarksMarginTop
                width: parent.width
                spacing: Theme.meetingHeadMarksGap
                Repeater {
                    model: marksBox.marks
                    MeetingBadge {
                        text: modelData.label
                        mono: modelData.mono
                        suffix: modelData.suffix || ""
                        accessibleName: modelData.accessibleName || ""
                        kind: modelData.tone
                        iconName: modelData.icon
                    }
                }
            }
        }
        Item {
            width: 1
            visible: (root.card.marks === undefined || root.card.marks.length === 0) && root.view !== ""
            // 06: подстрока панельных состояний — отступ снизу 12; без отметок над таблицей — 10.
            // Preserve the CSS header's half-pixel baseline phase before the inline install row.
            height: visible ? (root.panelState || root.view === "read-error" ? 12
                : Theme.meetingHeadMarksMarginBottom
                    + (root.card.diarizationInstallVisible === true ? Theme.diarInstallHeaderPhaseY : 0)) : 0
        }

        // ── расшифровка идёт (В4) ──────────────────────────────────────────
        Item {
            width: parent.width
            visible: root.view === "transcribing"
            height: visible ? progressCard.height + Theme.transcribeProgressMarginBottom : 0
            Rectangle {
                id: progressCard
                objectName: "meetingCardProgress"
                width: parent.width
                // Паддинги — от внутренней стороны рамки 1 (как border-box в макете).
                height: progressRow.implicitHeight + (Theme.transcribeProgressPaddingY + Theme.cardBorder) * 2
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: root.cardBorderColor
                RowLayout {
                    id: progressRow
                    x: Theme.cardBorder + Theme.transcribeProgressPaddingX
                    y: Theme.cardBorder + Theme.transcribeProgressPaddingY
                    width: parent.width - (Theme.transcribeProgressPaddingX + Theme.cardBorder) * 2
                    spacing: Theme.transcribeProgressGap
                    Icon { name: "clock"; size: Theme.transcribeProgressIcon; color: Theme.primary; Layout.alignment: Qt.AlignVCenter }
                    Column {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        // Строка 13.5 × 1.5 из PT Root UI и PT Mono на общей базовой линии (CSS):
                        // её коробка выше на полуразность (ascent − descent) двух шрифтов, а глифы
                        // стоят по базовой линии — отсюда 71.3 у карточки (tokens.md §3.8).
                        Item {
                            id: progressLine
                            width: parent.width
                            readonly property real lead: Theme.transcribeProgressTextSize * Theme.fontBodyLineHeight
                            readonly property real kUi: uiMetrics.ascent - uiMetrics.descent
                            readonly property real kMono: monoMetrics.ascent - monoMetrics.descent
                            readonly property real baseY: (lead + Math.max(kUi, kMono)) / 2
                            height: Math.max(lead + Math.abs(kUi - kMono) / 2, progressNames.y + progressNames.height)
                            FontMetrics { id: uiMetrics; font.family: Theme.fontUi; font.pixelSize: Theme.transcribeProgressTextSize }
                            FontMetrics { id: monoMetrics; font.family: Theme.fontMono; font.pixelSize: Theme.transcribeProgressTextSize }
                            Row {
                                id: progressTimes
                                Text {
                                    text: root.txt("progressDone")
                                    textFormat: Text.PlainText
                                    color: Theme.fg
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.transcribeProgressTextSize
                                    y: progressLine.baseY - baselineOffset
                                    renderType: Text.NativeRendering
                                }
                                Text {
                                    objectName: "meetingCardProgressDone"
                                    text: root.card.progressDone !== undefined ? root.card.progressDone : ""
                                    textFormat: Text.PlainText
                                    color: Theme.fg
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.transcribeProgressTextSize
                                    y: progressLine.baseY - baselineOffset
                                    font.letterSpacing: Theme.transcribeProgressTextSize * Theme.fontMonoInlineTracking
                                    renderType: Text.NativeRendering
                                }
                                Text {
                                    text: root.txt("progressOf")
                                    textFormat: Text.PlainText
                                    color: Theme.fg
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.transcribeProgressTextSize
                                    y: progressLine.baseY - baselineOffset
                                    renderType: Text.NativeRendering
                                }
                                Text {
                                    text: root.card.progressTotal !== undefined ? root.card.progressTotal : ""
                                    textFormat: Text.PlainText
                                    color: Theme.fg
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.transcribeProgressTextSize
                                    y: progressLine.baseY - baselineOffset
                                    font.letterSpacing: Theme.transcribeProgressTextSize * Theme.fontMonoInlineTracking
                                    renderType: Text.NativeRendering
                                }
                            }
                            Text {
                                id: progressNames
                                objectName: "meetingProgressNames"
                                x: progressTimes.width
                                y: progressLine.baseY - baselineOffset
                                width: Math.max(0, progressLine.width - x)
                                text: root.txt("progressNames")
                                textFormat: Text.PlainText
                                wrapMode: Text.WordWrap
                                color: Theme.fg
                                font.family: Theme.fontUi
                                font.pixelSize: Theme.transcribeProgressTextSize
                                lineHeight: Theme.transcribeProgressTextSize * Theme.fontBodyLineHeight
                                lineHeightMode: Text.FixedHeight
                                renderType: Text.NativeRendering
                            }
                        }
                        // FixedHeight оставляет leading снизу; CSS делит его пополам (как Step4Mic).
                        Item {
                            width: parent.width
                            height: caption.height
                            Text {
                                id: caption
                                y: (lineHeight - captionMetrics.height) / 2
                                width: parent.width
                                text: root.txt("progressCaption")
                                textFormat: Text.PlainText
                                wrapMode: Text.WordWrap
                                color: Theme.fgMuted
                                font.family: Theme.fontUi
                                font.pixelSize: Theme.transcribeProgressSubSize
                                lineHeight: Theme.transcribeProgressSubSize * Theme.fontBodyLineHeight
                                lineHeightMode: Text.FixedHeight
                                renderType: Text.NativeRendering
                            }
                            FontMetrics { id: captionMetrics; font: caption.font }
                        }
                        Item { width: 1; height: Theme.transcribeProgressTrackMarginTop }
                        Rectangle {
                            width: parent.width
                            height: Theme.progressHThin
                            radius: Theme.progressRadius
                            color: Theme.progressBg
                            Rectangle {
                                width: parent.width * Math.max(0, Math.min(100, root.card.percent || 0)) / 100
                                height: parent.height
                                radius: parent.radius
                                color: Theme.progressFill
                            }
                        }
                    }
                    AvButton {
                        objectName: "meetingCardCancel"
                        small: true
                        text: root.txt("cancel")
                        Layout.alignment: Qt.AlignVCenter
                        onClicked: if (root.bridge) root.bridge.cardAction(text)
                    }
                }
            }
        }

        // ── панель-сообщение вместо таблицы (06) ───────────────────────────
        Rectangle {
            id: statePanel
            objectName: "meetingCardPanel"
            readonly property var info: root.panelState ? root.card.panel : ({})
            visible: root.panelState
            width: parent.width
            // Паддинги — от внутренней стороны рамки 1 (как border-box в макете).
            height: visible ? Math.max(Theme.meetingStatePanelIcon, panelColumn.height) + (Theme.meetingStatePanelPaddingY + Theme.cardBorder) * 2 : 0
            color: Theme.bgSurface
            radius: Theme.cardRadius
            border.width: Theme.cardBorder
            border.color: root.cardBorderColor
            Icon {
                x: Theme.cardBorder + Theme.meetingStatePanelPaddingX
                y: Theme.cardBorder + Theme.meetingStatePanelPaddingY
                name: statePanel.info.icon !== undefined ? statePanel.info.icon : ""
                size: Theme.meetingStatePanelIcon
                color: statePanel.info.tone === "warning" ? Theme.warningInk
                    : statePanel.info.tone === "danger" ? Theme.dangerInk
                    : statePanel.info.tone === "primary" ? Theme.primary : Theme.fgMuted
            }
            Column {
                id: panelColumn
                x: Theme.cardBorder + Theme.meetingStatePanelPaddingX + Theme.meetingStatePanelIcon + Theme.meetingStatePanelGap
                y: Theme.cardBorder + Theme.meetingStatePanelPaddingY
                width: parent.width - x - Theme.meetingStatePanelPaddingX - Theme.cardBorder
                spacing: Theme.meetingStatePanelColGap
                Flow {
                    width: parent.width
                    Text {
                        id: panelTitle
                        objectName: "meetingCardPanelTitle"
                        width: Math.min(implicitWidth, parent.width)
                        // FixedHeight оставляет leading снизу; CSS делит его пополам (как Step4Mic).
                        topPadding: (lineHeight - panelTitleMetrics.height) / 2
                        height: lineCount * lineHeight
                        text: statePanel.info.title !== undefined ? statePanel.info.title : ""
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        color: Theme.fg
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingStatePanelTitleSize
                        lineHeight: Theme.meetingStatePanelTitleSize * Theme.fontBodyLineHeight
                        lineHeightMode: Text.FixedHeight
                        font.weight: Font.Medium
                        renderType: Text.NativeRendering
                        FontMetrics { id: panelTitleMetrics; font: panelTitle.font }
                    }
                    Text {
                        id: panelTitleMono
                        visible: text !== ""
                        // Базовая линия — общая с названием (PT Mono выше по ascent).
                        topPadding: panelTitle.topPadding + panelTitleMetrics.ascent - panelMonoMetrics.ascent
                        height: lineHeight
                        text: statePanel.info.titleMono !== undefined ? statePanel.info.titleMono : ""
                        textFormat: Text.PlainText
                        color: Theme.fg
                        font.family: Theme.fontMono
                        font.pixelSize: Theme.meetingStatePanelTitleSize
                        lineHeight: Theme.meetingStatePanelTitleSize * Theme.fontBodyLineHeight
                        lineHeightMode: Text.FixedHeight
                        font.weight: Font.Medium
                        font.letterSpacing: Theme.meetingStatePanelTitleSize * Theme.fontMonoInlineTracking
                        renderType: Text.NativeRendering
                        FontMetrics { id: panelMonoMetrics; font: panelTitleMono.font }
                    }
                }
                Text {
                    id: panelText
                    width: parent.width
                    topPadding: (lineHeight - panelTextMetrics.height) / 2
                    height: lineCount * lineHeight
                    text: statePanel.info.text !== undefined ? statePanel.info.text : ""
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: Theme.fgSecondary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.meetingStatePanelTextSize
                    lineHeight: Theme.meetingStatePanelTextSize * Theme.fontBodyLineHeight
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                    FontMetrics { id: panelTextMetrics; font: panelText.font }
                }
                Item {
                    id: buttonsBox
                    readonly property var buttons: statePanel.info.buttons !== undefined ? statePanel.info.buttons : []
                    visible: buttons.length > 0
                    width: parent.width
                    // Кнопка по токену 33.5; AvButton снапит её к 34 — полпикселя не в высоту панели.
                    height: visible ? panelButtons.height - (Math.round(Theme.buttonHeight) - Theme.buttonHeight) + Theme.meetingStatePanelActionsMarginTop : 0
                    Flow {
                        id: panelButtons
                        y: Theme.meetingStatePanelActionsMarginTop
                        width: parent.width
                        spacing: Theme.meetingStatePanelActionsGap
                        Repeater {
                            model: buttonsBox.buttons
                            AvButton {
                                objectName: "meetingCardPanelButton"
                                text: modelData.label
                                variant: modelData.primary ? "primary" : "secondary"
                                enabled: modelData.enabled
                                onClicked: if (root.bridge) root.bridge.cardAction(text)
                            }
                        }
                    }
                }
            }
        }

        NoteBanner {
            objectName: "meetingCardReadError"
            visible: root.view === "read-error"
            height: visible ? implicitHeight : 0
            variant: "error"
            iconName: "alert"
            body: root.txt("readError")
            AvButton { objectName: "meetingCardReadRetry"; small: true; text: root.txt("retry"); onClicked: if (root.bridge) root.bridge.retryCard() }
        }

        // policy-off: только чтение (06, tokens.md §3.9).
        Item {
            width: parent.width
            visible: root.card.policyOff === true
            height: visible ? policyNote.height + 8 : 0
            NoteBanner {
                id: policyNote
                objectName: "meetingCardPolicy"
                iconName: "lock"
                body: root.txt("policyOff")
            }
        }

        Item {
            id: redoRow
            objectName: "meetingDiarizationRedo"
            visible: root.redo.visible === true
            width: parent.width
            height: visible ? Math.max(redoLabel.height, redoControls.height) + Theme.diarInstallCardBottom : 0
            readonly property real lineTop: root.inlineLineTop(redoLabel.height, redoControls.height)
            readonly property real halfLeading: root.inlineHalfLeading(19.5, redoMetrics.height)
            FontMetrics { id: redoMetrics; font: redoLabel.font }
            Text {
                id: redoLabel
                objectName: "meetingRedoLabel"
                x: redoIcon.visible ? redoIcon.width + Theme.diarInstallIconGap : 0
                y: redoRow.lineTop + redoRow.halfLeading - 0.5
                width: Math.max(0, parent.width - x - (redoControls.width > 0 ? redoControls.width + 8 : 0))
                text: root.redo.text || ""
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: root.redo.tone === "danger" ? Theme.dangerInk : Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: 13
                lineHeight: 19.5
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                Accessible.role: root.redo.code === "cancelled" || root.redo.code === "done"
                    || root.redo.code === "no-audio" || root.redo.tone === "danger"
                    || (root.redo.code || "").indexOf("kept-") === 0 ? Accessible.AlertMessage : Accessible.StaticText
                Accessible.name: text
                Accessible.description: root.redo.storageText || ""
            }
            Icon {
                id: redoIcon
                objectName: "meetingRedoIcon"
                visible: (root.redo.icon || "") !== ""
                name: root.redo.icon || ""
                size: 12
                y: root.redo.icon === "alert" ? redoRow.lineTop + 3
                    : redoLabel.y + redoLabel.baselineOffset - height + 3 + 3 / 2
                color: root.redo.iconTone === "danger" ? Theme.dangerInk : Theme.successInk
                Accessible.ignored: true
            }
            Row {
                id: redoControls
                anchors.right: parent.right
                y: (parent.height - Theme.diarInstallCardBottom - height) / 2
                spacing: 8
                ProgressBar {
                    id: redoProgress
                    objectName: "meetingRedoProgress"
                    visible: root.redo.showProgress === true
                    width: 160
                    height: 4
                    padding: 0
                    anchors.verticalCenter: parent.verticalCenter
                    from: 0; to: 100
                    property int announcedPercent: 0
                    value: announcedPercent
                    onVisibleChanged: if (visible) announcedPercent = root.redo.percent || 0
                    Timer {
                        interval: 1000; repeat: true; running: redoProgress.visible
                        onTriggered: redoProgress.announcedPercent = root.redo.percent || 0
                    }
                    Accessible.role: Accessible.ProgressBar
                    Accessible.name: root.redo.progressName || ""
                    Accessible.ignored: !visible
                    background: Rectangle { color: Theme.bgSurface2; radius: Theme.progressRadius }
                    contentItem: Item {
                        Rectangle {
                            width: parent.width * (root.redo.percent || 0) / 100
                            height: 4; radius: Theme.progressRadius; color: Theme.primary
                        }
                    }
                }
                AvButton {
                    id: keepAction
                    objectName: "meetingRedoKeep"
                    visible: redoRow.visible && root.redo.canKeep === true
                    small: true
                    text: root.redo.keepAction || ""
                    Accessible.name: root.redo.keepName || ""
                    Accessible.description: root.redo.keepDescription || ""
                    KeyNavigation.backtab: moreButton
                    KeyNavigation.tab: redoAction
                    Keys.onReturnPressed: clicked()
                    Keys.onEnterPressed: clicked()
                    onClicked: if (root.bridge && root.bridge.keepDiarizationAsIs(root.redo.meetingId, root.redo.expectedGeneration))
                        Qt.callLater(root.focusRedo)
                }
                AvButton {
                    id: redoAction
                    objectName: "meetingRedoAction"
                    property bool ownedFocus: false
                    onActiveFocusChanged: if (activeFocus) ownedFocus = true; else if (visible) ownedFocus = false
                    onVisibleChanged: if (!visible && ownedFocus) {
                        ownedFocus = false
                        Qt.callLater(function() { table.forceActiveFocus(Qt.TabFocusReason) })
                    }
                    visible: redoRow.visible && text !== ""
                    enabled: root.redo.busy === true ? root.redo.canCancel === true : root.redo.canStart === true
                    small: true
                    text: root.redo.action || ""
                    Accessible.name: root.redo.actionName || ""
                    Accessible.description: root.redo.actionDescription || ""
                    KeyNavigation.backtab: keepAction.visible ? keepAction : moreButton
                    KeyNavigation.tab: table
                    Keys.onReturnPressed: if (enabled) clicked()
                    Keys.onEnterPressed: if (enabled) clicked()
                    onClicked: if (root.bridge) {
                        if (root.redo.busy === true) root.bridge.cancelDiarizationRedo(root.redo.meetingId, root.redo.expectedGeneration)
                        else if (root.redo.action === "Разделить по голосам") root.bridge.divideMeeting(root.redo.meetingId, root.redo.expectedGeneration)
                        else root.bridge.rediarizeMeeting(root.redo.meetingId, root.redo.expectedGeneration, false)
                    }
                }
            }
        }

        // ── полоса «Участники» (контент < 760) ─────────────────────────────
        Item {
            width: parent.width
            visible: root.card.showTable === true && root.hasParticipants && !root.wide
            height: visible ? strip.height + Theme.participantsStripMarginBottom : 0
            Rectangle {
                id: strip
                objectName: "meetingCardStrip"
                width: parent.width
                // participantsStripH — внешняя высота (замер); паддинги — внутри рамки 1.
                height: Math.max(Theme.participantsStripH, stripRow.implicitHeight + Theme.participantsStripPaddingTop + Theme.participantsStripPaddingBottom + Theme.cardBorder * 2)
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: root.cardBorderColor
                RowLayout {
                    id: stripRow
                    x: Theme.cardBorder + Theme.participantsStripPaddingLeft
                    width: parent.width - Theme.participantsStripPaddingLeft - Theme.participantsStripPaddingRight - Theme.cardBorder * 2
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Theme.participantsStripGap
                    Flow {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        spacing: Theme.participantsStripGap
                        Text {
                            text: root.txt("participantsStrip")
                            textFormat: Text.PlainText
                            color: Theme.fgMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.participantsStripLabelSize
                            lineHeight: Theme.participantsStripLabelSize * Theme.fontBodyLineHeight
                            lineHeightMode: Text.FixedHeight
                            height: Theme.participantsStripChipSize * 1.4
                            verticalAlignment: Text.AlignVCenter
                            renderType: Text.NativeRendering
                        }
                        Repeater {
                            model: participants.participants
                            Row {
                                spacing: Theme.speakerNumGap
                                MeetingSpeakerNumber { num: modelData.num || ""; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    text: modelData.label
                                    textFormat: Text.PlainText
                                    color: Theme.fg
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.participantsStripChipSize
                                    height: Theme.participantsStripChipSize * Theme.participantsQuoteLineHeight
                                    verticalAlignment: Text.AlignVCenter
                                    renderType: Text.NativeRendering
                                }
                            }
                        }
                    }
                    // При узком контенте открывает панель 300.
                    AvButton {
                        id: namesButton
                        objectName: "meetingCardNames"
                        KeyNavigation.backtab: table
                        KeyNavigation.tab: back
                        Keys.onReturnPressed: if (enabled) clicked()
                        Keys.onEnterPressed: if (enabled) clicked()
                        small: true
                        iconName: "people"
                        text: root.txt("names")
                        enabled: root.redo.busy !== true && root.card.live !== true && (root.card.participants || []).some(function(p) { return p.kind === "speaker" })
                        onClicked: root.openNames(namesButton)
                        Layout.alignment: Qt.AlignVCenter
                    }
                }
            }
        }

        Item {
            id: missingModels
            objectName: "meetingDiarizationMissing"
            visible: root.card.diarizationInstallVisible === true
            width: parent.width
            height: visible ? Math.max(installLabel.height, installButton.visible ? installButton.height : 0) + Theme.diarInstallCardBottom : 0
            readonly property bool progressVisible: ["downloading", "verifying", "installing"].indexOf(root.diarInstall.code) >= 0
            readonly property bool hasIcon: root.diarInstall.error === true || root.diarInstall.code === "installed"
            // Fixed line-height in Qt5 does not add CSS half-leading to the glyph baseline.
            readonly property real lineTop: root.inlineLineTop(installLabel.height,
                installButton.visible ? Theme.diarInstallCardControlH : 0)
            readonly property real halfLeading: root.inlineHalfLeading(
                Theme.fontSmallSize * Theme.fontBodyLineHeight, installMetrics.height)
            FontMetrics { id: installMetrics; font: installLabel.font }
            Icon {
                objectName: "meetingDiarizationStatusIcon"
                visible: missingModels.hasIcon
                name: root.diarInstall.error ? "alert" : "check"
                size: Theme.diarInstallIconSize
                color: root.diarInstall.error ? Theme.dangerInk : Theme.successInk
                // Inline vertical-middle includes half the 3 px top margin;
                // check ink has a 3 px bottom inset, alert a 1 px top inset.
                y: root.diarInstall.error ? missingModels.lineTop + 3
                    : installLabel.y + installLabel.baselineOffset - height + 3 + 3 / 2
                Accessible.ignored: true
            }
            Text {
                id: installLabel
                objectName: "meetingDiarizationLabel"
                x: missingModels.hasIcon ? Theme.diarInstallIconSize + Theme.diarInstallIconGap : 0
                width: Math.max(0, parent.width - x - (installControls.width > 0 ? installControls.width + Theme.diarInstallCardGap : 0))
                // NativeRendering rounds the origin; the half-pixel correction
                // selects the preceding pixel like the CSS line box.
                y: missingModels.lineTop + missingModels.halfLeading - 0.5
                text: root.card.diarizationHint || ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmallSize
                lineHeight: Theme.fontSmallSize * Theme.fontBodyLineHeight
                lineHeightMode: Text.FixedHeight
                color: root.diarInstall.error ? Theme.dangerInk : Theme.fgMuted
                renderType: Text.NativeRendering
                Accessible.name: text
                Accessible.role: missingModels.visible && (root.diarInstall.error === true || root.diarInstall.code === "installed") ? Accessible.AlertMessage : Accessible.StaticText
            }
            Row {
                id: installControls
                anchors.right: parent.right
                y: (parent.height - Theme.diarInstallCardBottom - height) / 2
                spacing: Theme.diarInstallCardGap
                DiarizationProgress {
                    objectName: "meetingDiarizationCardProgress"
                    width: Theme.diarInstallCardProgressW
                    anchors.verticalCenter: parent.verticalCenter
                    visible: missingModels.progressVisible
                    install: root.diarInstall
                }
                AvButton {
                    id: installButton
                    objectName: "meetingDiarizationInstall"
                    property bool ownedFocus: false
                    onActiveFocusChanged: if (activeFocus) ownedFocus = true; else if (visible) ownedFocus = false
                    onVisibleChanged: if (!visible && ownedFocus) {
                        ownedFocus = false
                        var target = table.visible && table.enabled && table.activeFocusOnTab ? table : back
                        if (target.visible && target.enabled) target.forceActiveFocus(Qt.TabFocusReason)
                    }
                    visible: missingModels.visible && text !== ""
                    Keys.onReturnPressed: clicked()
                    Keys.onEnterPressed: clicked()
                    KeyNavigation.tab: table.visible && table.enabled && table.activeFocusOnTab ? table : back
                    text: root.card.diarizationAction || ""
                    small: true
                    Accessible.name: root.diarInstall.actionName || ""
                    Accessible.description: root.diarInstall.error ? root.diarInstall.cardText : ""
                    onClicked: if (root.bridge) {
                        if (missingModels.progressVisible) root.bridge.cancelDiarizationInstall()
                        else root.bridge.requestDiarizationInstall()
                    }
                }
            }
        }

        // ── таблица реплик + панель «Участники» ────────────────────────────
        Row {
            visible: root.card.showTable === true
            width: parent.width
            spacing: Theme.participantsPanelGap

            Rectangle {
                id: tableCard
                objectName: "meetingCardTable"
                width: root.tableWidth
                height: (root.rowsDraining ? 0 : tableColumn.height) + Theme.cardBorder * 2
                clip: root.rowsDraining
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: root.cardBorderColor

                FocusScope {
                    id: table
                    objectName: "meetingCardTableFocus"
                    KeyNavigation.tab: null
                    x: Theme.cardBorder
                    y: Theme.cardBorder
                    width: parent.width - Theme.cardBorder * 2
                    height: tableColumn.height
                    activeFocusOnTab: rows.count > 0
                    Accessible.role: Accessible.List
                    Accessible.name: root.card.title !== undefined ? root.card.title : ""
                    // Одна позиция Tab на всю таблицу; стрелки ходят по репликам (spec §5.2).
                    onActiveFocusChanged: {
                        var item = activeFocus ? rows.itemAt(root.currentRow) : null
                        if (activeFocus && (!item || !item.activeFocus)) root.moveRow(0)
                    }
                    Keys.onBacktabPressed: {
                        if (redoAction.visible) redoAction.forceActiveFocus(Qt.BacktabFocusReason)
                        else if (installButton.visible) installButton.forceActiveFocus(Qt.BacktabFocusReason)
                        else if (root.card.showActions === true) moreButton.forceActiveFocus(Qt.BacktabFocusReason)
                        else {
                            var previous = table.nextItemInFocusChain(false)
                            if (previous) previous.forceActiveFocus(Qt.BacktabFocusReason)
                        }
                    }
                    Keys.onTabPressed: {
                        if (!root.wide && namesButton.enabled) namesButton.forceActiveFocus(Qt.TabFocusReason)
                        else if (!participants.focusFirst()) {
                            var next = table.nextItemInFocusChain(true)
                            if (next && next !== table) next.forceActiveFocus(Qt.TabFocusReason)
                            else event.accepted = false
                        }
                    }
                    Keys.onUpPressed: root.moveRow(-1)
                    Keys.onDownPressed: root.moveRow(1)
                    Keys.onPressed: {
                        if (event.key === Qt.Key_Home) { root.currentRow = 0; root.moveRow(0); event.accepted = true }
                        else if (event.key === Qt.Key_End) { root.currentRow = rows.count - 1; root.moveRow(0); event.accepted = true }
                    }

                    Column {
                        id: tableColumn
                        width: parent.width

                        // Свой столбик вместо Column: Column пересчитывает все строки на
                        // каждое добавление (9000 — ~65 мс), цепочка y — только новую.
                        Item {
                            id: rowsBox
                            width: parent.width
                            readonly property Item last: rows.count > 0 ? rows.itemAt(rows.count - 1) : null
                            height: last ? last.y + last.height : 0
                            Repeater {
                                id: rows
                                objectName: "meetingCardRows"
                                model: shownRows
                                delegate: MeetingTranscriptRow {
                                    property Item above: null
                                    y: above ? above.y + above.height : 0
                                    width: tableColumn.width
                                    first: index === 0
                                    kind: model.kind
                                    time: model.time
                                    who: model.who
                                    num: model.num
                                    flagText: model.flagText
                                    flagIcon: model.flagIcon
                                    textMuted: model.textMuted
                                    whoKind: model.whoKind
                                    text: model.text
                                    iconName: model.icon
                                    accessibleName: model.accessibleName
                                    Component.onCompleted: {
                                        // Строки только дописываются в конец и убираются с конца.
                                        above = index > 0 ? rows.itemAt(index - 1) : null
                                        root.rowsCreated += 1
                                    }
                                }
                            }
                        }

                        // «Дальше — по мере расшифровки» — последняя строка только в transcribing.
                        Item {
                            id: tail
                            objectName: "meetingCardTail"
                            visible: root.card.live === true
                            width: parent.width
                            readonly property real lead: rows.count > 0 ? Theme.transcriptRowDivider : 0
                            height: visible ? lead + Theme.transcriptRowPaddingY * 2 + tailText.height : 0
                            Rectangle { width: parent.width; height: Theme.transcriptRowDivider; color: Theme.borderSoft; visible: rows.count > 0 }
                            Text {
                                x: Theme.transcriptRowPaddingX
                                y: tail.lead + Theme.transcriptRowPaddingY + Theme.transcriptTimeOffsetTop
                                text: "…"
                                textFormat: Text.PlainText
                                color: Theme.transcriptTimeFg
                                font.family: Theme.fontMono
                                font.pixelSize: Theme.transcriptTimeSize
                                lineHeight: Theme.transcriptTimeSize * Theme.fontBodyLineHeight
                                lineHeightMode: Text.FixedHeight
                                renderType: Text.NativeRendering
                            }
                            Text {
                                id: tailText
                                objectName: "meetingCardTailText"
                                x: Theme.transcriptRowPaddingX + Theme.transcriptColTimeW + Theme.transcriptColGap * 2 + Theme.transcriptColWhoW
                                y: tail.lead + Theme.transcriptRowPaddingY
                                width: parent.width - x - Theme.transcriptRowPaddingX
                                text: root.txt("tail")
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                color: Theme.fgMuted
                                font.family: Theme.fontUi
                                font.pixelSize: Theme.transcriptTailSize
                                lineHeight: Theme.transcriptTextSize * Theme.transcriptTextLineHeight
                                lineHeightMode: Text.FixedHeight
                                verticalAlignment: Text.AlignVCenter
                                renderType: Text.NativeRendering
                            }
                        }
                    }
                }
            }

            MeetingParticipants {
                id: participants
                visible: root.wide && root.hasParticipants
                bridge: root.bridge
                tableFocusTarget: table
                onMergeRequested: root.openMerge(participant, origin, participants)
            }
        }
    }

    MeetingRediarizeDialog {
        id: redoDialog
        bridge: root.bridge
        current: root.redo
        onStarted: Qt.callLater(root.focusRedo)
    }

    Popup {
        id: namesDrawer
        objectName: "meetingParticipantsDrawer"
        parent: root.overlayViewport
        x: parent ? parent.width - width : 0
        y: 0
        width: Theme.participantsDrawerW
        height: parent ? parent.height : drawerPanel.height
        padding: 0
        z: Theme.popoverZ - 1
        modal: true
        dim: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onOpened: drawerPanel.focusFirst()
        onClosed: Qt.callLater(function() { if (root.namesOrigin && root.namesOrigin.visible) root.namesOrigin.forceActiveFocus(Qt.TabFocusReason) })
        background: Rectangle {
            OverlaySurface {
                anchors.fill: parent
                surfaceWidth: parent.width
                surfaceHeight: parent.height
                surfaceRadius: 0
                surfaceColor: Theme.participantsDrawerBg
                shadowX: Theme.shadowMeetingDrawerX
                shadowY: Theme.shadowMeetingDrawerY
                shadowRadius: Theme.shadowMeetingDrawerRadius
                shadowColor: Theme.shadowMeetingDrawerColor
                shadowSourceColor: Theme.participantsDrawerBg
            }
            color: Theme.participantsDrawerBg
        }
        contentItem: Item {
            Flickable {
                anchors.fill: parent
                id: participantsScroll
                objectName: "meetingParticipantsScroll"
                contentWidth: width
                contentHeight: drawerPanel.height
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                function revealFocus() {
                    var item = root.Window.window ? root.Window.window.activeFocusItem : null
                    var ancestor = item
                    while (ancestor && ancestor !== drawerPanel) ancestor = ancestor.parent
                    if (!ancestor || !item) return
                    var point = item.mapToItem(drawerPanel, 0, 0)
                    var margin = Theme.focusOffset + Theme.focusWidth
                    var end = Math.max(0, contentHeight - height)
                    if (point.y - margin < contentY) contentY = Math.max(0, point.y - margin)
                    else if (point.y + item.height + margin > contentY + height)
                        contentY = Math.min(end, point.y + item.height + margin - height)
                }
                Connections {
                    target: root.Window.window
                    function onActiveFocusItemChanged() { Qt.callLater(participantsScroll.revealFocus) }
                }
                MeetingParticipants {
                    id: drawerPanel
                    bridge: root.bridge
                    drawer: true
                    onCloseRequested: namesDrawer.close()
                    onMergeRequested: root.openMerge(participant, origin, drawerPanel)
                }
            }
            Rectangle {
                objectName: "meetingParticipantsBorder"
                width: Theme.participantsDrawerBorderLeft
                height: parent.height
                color: Theme.border
                z: 1
            }
        }
        enter: Transition {
            NumberAnimation { property: "x"; from: namesDrawer.parent ? namesDrawer.parent.width : 0; to: namesDrawer.parent ? namesDrawer.parent.width - namesDrawer.width : 0; duration: Theme.durationEnter; easing.type: Easing.Bezier; easing.bezierCurve: Theme.easingEnter.concat([1, 1]) }
        }
        exit: Transition {
            NumberAnimation { property: "x"; to: namesDrawer.parent ? namesDrawer.parent.width : 0; duration: Theme.durationExit; easing.type: Easing.Bezier; easing.bezierCurve: Theme.easingExit.concat([1, 1]) }
        }
    }
    MeetingMergePopover {
        id: mergePopover
        parent: Overlay.overlay
        bridge: root.bridge
    }
    MeetingSpeakerToast {
        id: speakerToast
        bridge: root.bridge
        parent: root.overlayViewport || root
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Theme.toastBottom
        maxWidth: root.width - Theme.toastMaxWInset
    }

    MeetingRenameDialog {
        id: renameDialog
        bridge: root.bridge
        fallbackFocusItem: back
    }

    MeetingSaveDialog {
        id: saveDialog
        bridge: root.bridge
        returnFocusItem: saveButton
        fallbackFocusItem: back
    }
}
