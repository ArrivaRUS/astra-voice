// Карточка встречи A «Документ» — design/meetings/spec.md §3.7/§4.7/§5.2, §7;
// значения — design/meetings/tokens.md §3. Макеты 05-meeting-A.html, 06-meeting-states.html.
// M4: без имён и номеров спикеров (M7), без переименования/удаления (M5), без плеера (Could).
// Вся карточка живёт в общем Flickable окна (§5.2): своей прокрутки нет.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

FocusScope {
    id: root
    objectName: "meetingCard"

    property var bridge: null
    readonly property var card: bridge && bridge.card !== undefined && bridge.card.open === true ? bridge.card : ({})
    readonly property var texts: bridge && bridge.cardTexts !== undefined ? bridge.cardTexts : ({})
    readonly property var rowsModel: bridge && bridge.cardRows !== undefined ? bridge.cardRows : null
    readonly property string view: card.view !== undefined ? card.view : ""
    // Точка 760 по ширине контента (spec §5.2): шире — панель «Участники» справа.
    readonly property bool wide: width >= 760
    readonly property bool panelState: card.panel !== undefined && card.panel.title !== undefined
    readonly property real tableWidth: wide ? width - Theme.participantsPanelGap - Theme.participantsPanelW : width
    // Рамка карточек: в тёмной теме border полупрозрачный; Qt не кладёт под рамку заливку,
    // поэтому смешиваем его с bgSurface заранее (CSS border-box), как OverlaySurface (M3).
    readonly property color cardBorderColor: Qt.tint(Theme.bgSurface, Theme.border)
    // Сколько строк таблицы создано: инкрементальная модель не пересоздаёт старые.
    property int rowsCreated: 0
    property int currentRow: 0

    signal backRequested()

    width: parent ? parent.width : Theme.sizeContentColMax
    implicitHeight: column.implicitHeight

    function txt(key) { return texts[key] !== undefined ? texts[key] : "" }
    function focusBack(reason) { back.forceActiveFocus(reason === undefined ? Qt.TabFocusReason : reason) }
    function openSaveDialog() { if (root.card.showActions === true) saveDialog.open() }
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
            height: Math.max(headText.height, headActions.visible ? headActions.height : 0)

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
                        // D3 «Переименовать» — M5: значок виден, но недоступен.
                        AbstractButton {
                            objectName: "meetingCardEdit"
                            anchors.centerIn: parent
                            width: Theme.meetingHeadEditIcon
                            height: Theme.meetingHeadEditIcon
                            enabled: false
                            focusPolicy: Qt.NoFocus
                            Accessible.name: root.txt("rename")
                            contentItem: Icon { name: "edit"; size: Theme.meetingHeadEditIcon; color: Theme.fgMuted }
                            background: Item {}
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
                    Text {
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
                        // Esc закрывает меню, фокус — обратно на «⋯» (spec §5.2).
                        onClosed: moreButton.forceActiveFocus(Qt.TabFocusReason)
                        MenuItem { text: root.txt("rename"); enabled: false }
                        MenuItem { text: root.txt("names"); enabled: false }
                        MenuSeparator {}
                        // D2 «Удалить встречу» — M5.
                        MenuItem { text: root.txt("delete"); enabled: false }
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
            height: visible ? (root.panelState || root.view === "read-error" ? 12 : Theme.meetingHeadMarksMarginBottom) : 0
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
                            height: lead + Math.abs(kUi - kMono) / 2
                            FontMetrics { id: uiMetrics; font.family: Theme.fontUi; font.pixelSize: Theme.transcribeProgressTextSize }
                            FontMetrics { id: monoMetrics; font.family: Theme.fontMono; font.pixelSize: Theme.transcribeProgressTextSize }
                            Row {
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
            AvButton { small: true; text: root.txt("retry"); onClicked: if (root.bridge) root.bridge.retryCard() }
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

        // ── полоса «Участники» (контент < 760) ─────────────────────────────
        Item {
            width: parent.width
            visible: root.card.showTable === true && !root.wide
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
                            model: root.card.participants !== undefined ? root.card.participants : []
                            Text {
                                text: modelData.label
                                textFormat: Text.PlainText
                                color: Theme.fg
                                font.family: Theme.fontUi
                                font.pixelSize: Theme.participantsStripChipSize
                                lineHeight: Theme.participantsStripChipSize * Theme.fontBodyLineHeight
                                lineHeightMode: Text.FixedHeight
                                height: Theme.participantsStripChipSize * 1.4
                                verticalAlignment: Text.AlignVCenter
                                renderType: Text.NativeRendering
                            }
                        }
                    }
                    // Имена — M7: кнопка видна, но недоступна (полей для правки в M4 нет).
                    AvButton {
                        objectName: "meetingCardNames"
                        small: true
                        iconName: "people"
                        text: root.txt("names")
                        enabled: false
                        Layout.alignment: Qt.AlignVCenter
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
                        var previous = table.nextItemInFocusChain(false)
                        if (previous) previous.forceActiveFocus(Qt.BacktabFocusReason)
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

            // Панель «Участники» 232 (контент ≥ 760). M4: «Я» и «Собеседники», без полей имён.
            Rectangle {
                id: participants
                objectName: "meetingCardParticipants"
                visible: root.wide
                width: Theme.participantsPanelW
                height: participantsColumn.height + Theme.cardBorder * 2
                color: Theme.bgSurface
                radius: Theme.cardRadius
                border.width: Theme.cardBorder
                border.color: root.cardBorderColor
                Column {
                    id: participantsColumn
                    x: Theme.cardBorder
                    y: Theme.cardBorder
                    width: parent.width - Theme.cardBorder * 2
                    Item {
                        width: parent.width
                        height: participantsTitle.height + Theme.participantsHeadPaddingY * 2
                        Text {
                            id: participantsTitle
                            x: Theme.participantsHeadPaddingX
                            anchors.verticalCenter: parent.verticalCenter
                            // FixedHeight оставляет leading снизу; CSS делит его пополам (как Step4Mic).
                            topPadding: (lineHeight - participantsTitleMetrics.height) / 2
                            height: lineHeight
                            text: root.txt("participants")
                            textFormat: Text.PlainText
                            color: Theme.fg
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.participantsTitleSize
                            lineHeight: Theme.participantsTitleSize * Theme.fontBodyLineHeight
                            lineHeightMode: Text.FixedHeight
                            font.weight: Font.Medium
                            renderType: Text.NativeRendering
                            FontMetrics { id: participantsTitleMetrics; font: participantsTitle.font }
                        }
                        Icon {
                            x: parent.width - Theme.participantsHeadPaddingX - width
                            anchors.verticalCenter: parent.verticalCenter
                            name: "people"
                            size: Theme.participantsHeadIcon
                            color: Theme.fgMuted
                        }
                    }
                    Repeater {
                        model: root.card.participants !== undefined ? root.card.participants : []
                        Item {
                            objectName: "meetingCardParticipant"
                            width: participantsColumn.width
                            height: Theme.participantsItemPaddingY * 2 + Theme.cardBorder + itemColumn.height
                            Rectangle { width: parent.width; height: Theme.cardBorder; color: Theme.borderSoft }
                            Column {
                                id: itemColumn
                                x: Theme.participantsItemPaddingX
                                y: Theme.cardBorder + Theme.participantsItemPaddingY
                                width: parent.width - Theme.participantsItemPaddingX * 2
                                Item {
                                    width: parent.width
                                    height: name.height
                                    Text {
                                        id: name
                                        width: Math.min(implicitWidth, parent.width - minutes.width - 6)
                                        text: modelData.label
                                        textFormat: Text.PlainText
                                        elide: Text.ElideRight
                                        color: modelData.kind === "me" ? Theme.transcriptMeFg : Theme.fg
                                        font.family: Theme.fontUi
                                        font.pixelSize: Theme.participantsNameSize
                                        lineHeight: Theme.participantsNameSize * Theme.fontBodyLineHeight
                                        lineHeightMode: Text.FixedHeight
                                        font.weight: Font.Medium
                                        renderType: Text.NativeRendering
                                    }
                                    Text {
                                        id: minutes
                                        objectName: "meetingCardMinutes"
                                        x: parent.width - width
                                        y: name.baselineOffset - baselineOffset
                                        text: modelData.minutes
                                        textFormat: Text.PlainText
                                        color: Theme.fgMuted
                                        font.family: Theme.fontMono
                                        font.pixelSize: Theme.participantsMinutesSize
                                        lineHeight: Theme.participantsMinutesSize * Theme.fontBodyLineHeight
                                        lineHeightMode: Text.FixedHeight
                                        font.letterSpacing: Theme.participantsMinutesSize * Theme.fontMonoInlineTracking
                                        renderType: Text.NativeRendering
                                    }
                                }
                                Text {
                                    visible: modelData.kind === "me"
                                    width: parent.width
                                    text: root.txt("meCaption")
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WordWrap
                                    color: Theme.fgMuted
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.participantsCaptionSize
                                    lineHeight: Theme.participantsCaptionSize * Theme.fontBodyLineHeight
                                    lineHeightMode: Text.FixedHeight
                                    renderType: Text.NativeRendering
                                }
                            }
                        }
                    }
                    Item {
                        objectName: "meetingCardNamesLater"
                        visible: root.card.live === true
                        width: parent.width
                        height: visible ? Theme.cardBorder + Theme.participantsFootPaddingY + laterText.height + Theme.participantsCaptionPaddingBottom : 0
                        Rectangle { width: parent.width; height: Theme.cardBorder; color: Theme.borderSoft }
                        Text {
                            id: laterText
                            x: Theme.participantsFootPaddingX
                            y: Theme.cardBorder + Theme.participantsFootPaddingY
                            width: parent.width - Theme.participantsFootPaddingX * 2
                            text: root.txt("namesLater")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: Theme.fgMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.participantsCaptionSize
                            lineHeight: Theme.participantsCaptionSize * Theme.fontBodyLineHeight
                            lineHeightMode: Text.FixedHeight
                            renderType: Text.NativeRendering
                        }
                    }
                }
            }
        }
    }

    MeetingSaveDialog {
        id: saveDialog
        bridge: root.bridge
        returnFocusItem: saveButton
        fallbackFocusItem: back
    }
}
