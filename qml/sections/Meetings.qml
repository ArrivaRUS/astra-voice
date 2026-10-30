// E13 M1 shell: design/meetings/spec.md §3.1/4.1. No capture or ASR actions.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import ".."
import "../components"

Column {
    id: root
    objectName: "meetingsSection"
    readonly property var bridge: typeof meetingBridge !== "undefined" && meetingBridge !== null ? meetingBridge : null
    readonly property string sectionState: bridge ? bridge.sectionState : "off"
    readonly property bool featureEnabled: bridge ? bridge.enabled : false
    readonly property var rows: bridge ? bridge.meetings : []
    readonly property bool loading: bridge ? bridge.loading : false
    readonly property bool readError: bridge ? bridge.readError : false
    readonly property bool showList: !loading && (featureEnabled || rows.length > 0) || (bridge && bridge.loadingVisible) || readError
    width: parent ? parent.width : Theme.sizeContentColMax
    spacing: Theme.spaceGroupGap
    function txt(key, fallback) { return bridge && bridge.texts[key] !== undefined ? bridge.texts[key] : fallback }
    function value(key, fallback) { return bridge && bridge.settings[key] !== undefined ? bridge.settings[key] : fallback }
    function editable(key) { return bridge ? bridge.locked[key] !== true : true }
    function commit(key, value) { if (bridge) bridge.setSetting(key, value) }
    readonly property bool detectorAvailable: bridge ? bridge.detectorEnabled : false
    Component.onCompleted: if (bridge) bridge.retry()
    Connections {
        target: root.bridge
        function onChanged() {
            if (root.bridge && root.bridge.saveError !== undefined && root.bridge.saveError !== "") {
                Qt.callLater(function() {
                    var window = root.Window.window
                    if (window && typeof window.revealPageItem === "function") window.revealPageItem(saveError)
                })
            }
        }
    }

    Item {
        width: parent.width
        visible: root.featureEnabled && root.bridge !== null && root.bridge.policyNote !== ""
        height: visible ? policyNote.implicitHeight + Theme.meetingSectionPolicyNoteMarginTop + Theme.meetingSectionPolicyNoteMarginBottom : 0
        NoteBanner {
            id: policyNote
            objectName: "meetingPolicyNote"
            y: Theme.meetingSectionPolicyNoteMarginTop
            iconName: "lock"
            body: root.bridge ? root.bridge.policyNote : ""
        }
    }

    NoteBanner {
        id: saveError
        objectName: "meetingSaveError"
        visible: root.bridge && root.bridge.saveError !== undefined ? root.bridge.saveError !== "" : false
        variant: "error"
        iconName: "alert"
        body: root.bridge && root.bridge.saveError !== undefined ? root.bridge.saveError : ""
    }

    NoteBanner {
        visible: root.bridge ? root.bridge.flyWarning !== "" : false
        variant: "warn"
        iconName: "alert"
        body: root.bridge ? root.bridge.flyWarning : ""
    }

    Rectangle {
        id: entry
        objectName: "meetingOffEntry"
        width: parent.width
        visible: !root.featureEnabled
        height: visible ? entryText.implicitHeight + Theme.meetingEntryPaddingY * 2 : 0
        radius: Theme.cardRadius
        color: Theme.bgSurface
        border.width: Theme.cardBorder
        border.color: Theme.border
        Rectangle {
            x: Theme.meetingEntryPaddingX
            y: Theme.meetingEntryPaddingY
            width: Theme.meetingEntryIconTile
            height: Theme.meetingEntryIconTile
            radius: Theme.meetingEntryIconTileRadius
            color: Theme.meetingEntryIconTileBg
            Icon { anchors.centerIn: parent; name: "meeting"; size: Theme.meetingEntryIcon; color: Theme.meetingEntryIconFg }
        }
        Column {
            id: entryText
            x: Theme.meetingEntryPaddingX + Theme.meetingEntryIconTile + Theme.meetingEntryGap
            y: Theme.meetingEntryPaddingY
            width: parent.width - x - Theme.meetingEntryPaddingX
            spacing: Theme.meetingEntryColGap
            Text {
                text: root.txt("offTitle", "Запись встреч выключена")
                textFormat: Text.PlainText
                color: Theme.fg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontH3SubsectionSize
                font.weight: Font.Medium
                renderType: Text.NativeRendering
            }
            Text {
                width: Math.min(parent.width, Theme.meetingEntryTextMaxW)
                text: root.txt("offDescription", "Astra Voice может записать встречу — ваш микрофон и звук собеседников из программы видеосвязи — и сделать из записи текст прямо на этом компьютере, без интернета. Запись начинается, только когда вы нажмёте «Начать запись».")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Theme.fgSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.meetingEntryTextSize
                lineHeightMode: Text.FixedHeight
                lineHeight: Theme.meetingEntryTextSize * Theme.meetingEntryTextLineHeight
                renderType: Text.NativeRendering
            }
            Flow {
                width: parent.width
                spacing: 8
                AvButton {
                    objectName: "meetingEnable"
                    text: root.txt("enable", "Включить запись встреч")
                    variant: "primary"
                    enabled: root.bridge ? root.bridge.canEnable : false
                    onClicked: if (root.bridge) root.bridge.openResponsibility()
                }
                Row {
                    spacing: Theme.meetingEntryPolicyGap
                    visible: root.sectionState === "policy-off"
                    height: Theme.buttonHeight
                    Icon { name: "lock"; size: Theme.meetingEntryPolicyIcon; color: Theme.fgSecondary; anchors.verticalCenter: parent.verticalCenter }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.txt("policyOff", "Запись встреч отключена администратором")
                        textFormat: Text.PlainText
                        color: Theme.fgSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingEntryPolicySize
                        renderType: Text.NativeRendering
                    }
                }
            }
            Text {
                width: parent.width
                visible: root.bridge ? root.bridge.policyInvalidText !== "" : false
                height: visible ? implicitHeight : 0
                text: root.bridge ? root.bridge.policyInvalidText : ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Theme.dangerInk
                font.family: Theme.fontUi
                font.pixelSize: Theme.meetingEntryErrorSize
                renderType: Text.NativeRendering
            }
            Text {
                width: parent.width
                text: root.txt("offCaption", "Пока функция выключена, программа не следит за звонками и ничего не записывает.")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.meetingEntryFootSize
                renderType: Text.NativeRendering
            }
        }
    }

    SettingGroup {
        objectName: "meetingList"
        width: parent.width
        visible: root.showList
        title: root.txt("recordedHeading", "ЗАПИСАННЫЕ ВСТРЕЧИ")
        Item {
            width: parent.width
            visible: root.loading
            height: visible && root.bridge && root.bridge.loadingVisible ? 44 * 3 : 0
            Column {
                width: parent.width
                visible: root.bridge ? root.bridge.loadingVisible : false
                Repeater {
                    model: 3
                    Rectangle {
                        width: parent.width
                        height: 44
                        color: Theme.meetingListSkeletonBg
                        Rectangle { x: Theme.meetingRowPaddingLeft; y: 10; width: parent.width * 0.46; height: Theme.meetingListSkeletonBarH; radius: Theme.meetingListSkeletonRadius; color: Theme.borderSoft }
                        Rectangle { x: Theme.meetingRowPaddingLeft; y: 10 + Theme.meetingListSkeletonBarH + Theme.meetingListSkeletonGap; width: parent.width * 0.30; height: Theme.meetingListSkeletonBar2H; radius: Theme.meetingListSkeletonRadius; color: Theme.borderSoft }
                    }
                }
            }
        }
        Item {
            width: parent.width
            visible: !root.loading && !root.readError && root.featureEnabled && root.rows.length === 0
            height: visible ? Math.max(Theme.meetingListEmptyMinH, emptyText.implicitHeight + Theme.cardRowPaddingY * 2) : 0
            Text {
                id: emptyText
                x: Theme.cardRowPaddingX
                width: parent.width - x * 2
                anchors.verticalCenter: parent.verticalCenter
                text: root.txt("empty", "Встреч пока нет. Нажмите «Записать встречу» или выберите этот пункт в меню значка в трее.")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.meetingListEmptySize
                renderType: Text.NativeRendering
            }
        }
        Item {
            width: parent.width
            visible: root.readError
            height: visible ? errorLine.implicitHeight + Theme.cardRowPaddingY * 2 : 0
            RowLayout {
                id: errorLine
                x: Theme.cardRowPaddingX
                y: Theme.cardRowPaddingY
                width: parent.width - x * 2
                spacing: Theme.meetingRowGap
                Icon { name: "alert"; size: 16; color: Theme.dangerInk }
                Text {
                    Layout.fillWidth: true
                    text: root.txt("error", "Не удалось прочитать папку встреч. Проверьте, что диск подключён и доступен.")
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: Theme.dangerInk
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.meetingListEmptySize
                    renderType: Text.NativeRendering
                }
                AvButton { text: root.txt("retry", "Повторить"); small: true; onClicked: if (root.bridge) root.bridge.retry() }
            }
        }
        Repeater {
            model: !root.loading && !root.readError ? root.rows : []
            delegate: Item {
                id: meetingRow
                readonly property var row: modelData
                width: parent.width
                height: Math.max(Theme.meetingRowMinH, rowBody.implicitHeight + Theme.meetingRowPaddingTop + Theme.meetingRowPaddingBottom)
                Rectangle { anchors.fill: parent; color: meetingRow.row.active ? Theme.meetingRowLiveBg : "transparent" }
                Rectangle { width: parent.width; height: 1; color: Theme.borderSoft; visible: index > 0 }
                RowLayout {
                    id: rowBody
                    x: Theme.meetingRowPaddingLeft
                    y: Theme.meetingRowPaddingTop
                    width: parent.width - Theme.meetingRowPaddingLeft - Theme.meetingRowPaddingRight
                    spacing: Theme.meetingRowGap
                    Column {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Text {
                            id: meetingName
                            objectName: "meetingName"
                            width: parent.width
                            text: meetingRow.row.title
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                            color: Theme.fg
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.meetingRowNameSize
                            renderType: Text.NativeRendering
                            ToolTip {
                                parent: meetingName
                                visible: nameHover.containsMouse && meetingName.truncated
                                contentItem: Text {
                                    text: meetingName.text
                                    textFormat: Text.PlainText
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.fontCaptionSize
                                    color: Theme.fg
                                    renderType: Text.NativeRendering
                                }
                            }
                            MouseArea { id: nameHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                        }
                        Text {
                            width: parent.width
                            text: meetingRow.row.subtitle
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                            color: Theme.fgMuted
                            font.family: Theme.fontMono
                            font.pixelSize: Theme.meetingRowMetaSize
                            font.letterSpacing: Theme.meetingRowMetaSize * Theme.fontMonoInlineTracking
                            renderType: Text.NativeRendering
                        }
                    }
                    Rectangle {
                        objectName: "meetingBadge"
                        readonly property string kind: meetingRow.row.badgeKind
                        color: kind === "success" ? Theme.successBg : (kind === "danger" || kind === "error") ? Theme.dangerBg : kind === "warning" ? Theme.warningBg : kind === "primary" ? Theme.primaryBg : Theme.bgSurface2
                        radius: Theme.badgeRadius
                        implicitWidth: badgeLabel.implicitWidth + Theme.badgePaddingX * 2
                        implicitHeight: badgeLabel.implicitHeight + Theme.badgePaddingY * 2
                        Text {
                            id: badgeLabel
                            anchors.centerIn: parent
                            text: meetingRow.row.badge
                            textFormat: Text.PlainText
                            color: parent.kind === "success" ? Theme.successInk : (parent.kind === "danger" || parent.kind === "error") ? Theme.dangerInk : parent.kind === "warning" ? Theme.warningInk : parent.kind === "primary" ? Theme.primary : Theme.fgSecondary
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontBadgeSize
                            font.weight: Font.Medium
                            renderType: Text.NativeRendering
                        }
                    }
                    AvButton {
                        small: true
                        text: meetingRow.row.action
                        visible: text !== ""
                        onClicked: if (root.bridge) root.bridge.requestMeetingAction(meetingRow.row.id, text)
                    }
                    AvButton {
                        objectName: "meetingMore"
                        width: Theme.meetingRowMoreButton
                        height: Theme.meetingRowMoreButton
                        leftPadding: 0
                        rightPadding: 0
                        small: true
                        iconName: "more"
                        iconFill: true
                        variant: "ghost"
                        visible: meetingRow.row.actions.length > 0
                        Accessible.name: "⋯"
                        onClicked: rowMenu.open()
                        Menu {
                            id: rowMenu
                            width: Theme.popoverMinW
                            Instantiator {
                                model: meetingRow.row.actions
                                delegate: MenuItem {
                                    text: modelData
                                    onTriggered: if (root.bridge) root.bridge.requestMeetingAction(meetingRow.row.id, text)
                                }
                                onObjectAdded: rowMenu.insertItem(index, object)
                                onObjectRemoved: rowMenu.removeItem(object)
                            }
                        }
                    }
                }
                Rectangle {
                    width: parent.width
                    height: Theme.progressHThin
                    anchors.bottom: parent.bottom
                    visible: meetingRow.row.progress > 0
                    color: Theme.meetingRowProgressBg
                    Rectangle { width: parent.width * meetingRow.row.progress / 100; height: parent.height; color: Theme.meetingRowProgressFill }
                }
            }
        }
        Item {
            width: parent.width
            visible: root.rows.length > 0 && !root.loading && !root.readError
            height: visible ? Theme.meetingListFootH : 0
            Rectangle { width: parent.width; height: 1; color: Theme.borderSoft }
            Text {
                x: Theme.meetingListFootPaddingX
                anchors.verticalCenter: parent.verticalCenter
                text: root.bridge ? root.bridge.totalText : ""
                textFormat: Text.PlainText
                color: Theme.fgMuted
                font.family: Theme.fontMono
                font.pixelSize: Theme.meetingListFootSize
                renderType: Text.NativeRendering
            }
        }
    }

    Column {
        width: parent.width
        visible: root.featureEnabled
        spacing: Theme.spaceGroupGap
        SettingGroup {
            width: parent.width
            title: root.txt("recordHeading", "ЗАПИСЬ")
            SettingRow {
                width: parent.width
                divider: false
                label: root.txt("offer", "Предлагать запись, когда начинается звонок")
                sub: root.txt("offerCaption", "Программа заметит звонок и спросит. Сама запись не начнётся")
                rowEnabled: root.editable("meeting_offer_on_call") && root.detectorAvailable
                toggle: offerToggle
                AvToggle {
                    id: offerToggle
                    objectName: "meetingOfferToggle"
                    checked: root.value("meeting_offer_on_call", false)
                    locked: !root.editable("meeting_offer_on_call") || !root.detectorAvailable
                    onToggled: {
                        root.commit("meeting_offer_on_call", checked)
                        checked = Qt.binding(function() { return root.value("meeting_offer_on_call", false) })
                    }
                }
            }
            SettingRow {
                width: parent.width
                label: root.txt("knownApps", "Какие звонки узнаются")
                sub: root.bridge ? root.bridge.knownAppsText : "DION · звонок в браузере"
            }
        }
        SettingGroup {
            width: parent.width
            title: root.txt("noticeHeading", "ПРЕДУПРЕЖДЕНИЕ УЧАСТНИКОВ")
            MeetingSettingField {
                width: parent.width
                divider: false
                objectName: "meetingNoticeField"
                label: root.txt("noticeLabel", "Текст для чата встречи")
                sub: root.txt("noticeCaption", "Показывается при каждом начале записи — с кнопкой «Скопировать для чата»")
                value: root.value("meeting_notice_text", "Я веду запись и расшифровку этой встречи для протокола.")
                rowEnabled: root.editable("meeting_notice_text")
                onCommitted: root.commit("meeting_notice_text", value)
            }
            MeetingSettingField {
                width: parent.width
                label: root.txt("inviteLabel", "Текст для приглашения на встречу")
                sub: root.txt("inviteCaption", "Предупредить заранее надёжнее всего — добавьте этот текст в приглашение")
                value: root.value("meeting_invite_text", "На встрече может вестись запись и расшифровка для протокола. Запись хранится только на компьютере участника, который её ведёт.")
                rowEnabled: root.editable("meeting_invite_text")
                onCommitted: root.commit("meeting_invite_text", value)
            }
            SettingRow {
                width: parent.width
                AvButton {
                    id: copyInvite
                    text: copyTimer.running ? root.txt("copied", "✓ Скопировано") : root.txt("copy", "Скопировать")
                    iconName: "copy"
                    onClicked: if (root.bridge && root.bridge.copyInviteText()) copyTimer.restart()
                    Timer { id: copyTimer; interval: 1200 }
                }
            }
        }
        SettingGroup {
            width: parent.width
            title: root.txt("transcriptionHeading", "РАСШИФРОВКА")
            SettingRow {
                width: parent.width
                divider: false
                label: root.txt("modelLabel", "Модель для расшифровки")
                sub: root.txt("modelMissing", "Не установлена — без неё расшифровки не будет")
                AvButton { small: true; text: root.txt("install", "Установить"); onClicked: if (root.bridge) root.bridge.requestMeetingAction("", text) }
            }
            SettingRow {
                width: parent.width
                label: root.txt("transcribeImmediately", "Расшифровывать сразу после записи")
                rowEnabled: root.editable("meeting_transcribe_immediately")
                toggle: transcribeToggle
                AvToggle { id: transcribeToggle; checked: root.value("meeting_transcribe_immediately", true); locked: !root.editable("meeting_transcribe_immediately"); onToggled: root.commit("meeting_transcribe_immediately", checked) }
            }
            SettingRow {
                width: parent.width
                label: root.txt("diarization", "Разделять собеседников по голосам")
                sub: root.txt("diarizationCaption", "Собеседники станут «Спикер 1, 2…». Голоса не запоминаются — ни между встречами, ни внутри встречи")
                rowEnabled: root.editable("meeting_diarization")
                toggle: diarToggle
                AvToggle { id: diarToggle; checked: root.value("meeting_diarization", true); locked: !root.editable("meeting_diarization"); onToggled: root.commit("meeting_diarization", checked) }
            }
            MeetingSettingField {
                width: parent.width
                objectName: "meetingSelfLabelField"
                label: root.txt("selfLabel", "Как подписывать вас в расшифровке")
                stacked: false
                fieldWidth: Theme.meetingSectionFieldMeW
                maximumLength: 40
                value: root.value("meeting_self_label", "Я")
                rowEnabled: root.editable("meeting_self_label")
                onCommitted: root.commit("meeting_self_label", value)
            }
        }
        SettingGroup {
            width: parent.width
            title: root.txt("retentionHeading", "ХРАНЕНИЕ")
            SettingRow {
                width: parent.width
                divider: false
                label: root.txt("audioRetention", "Хранить запись звука после расшифровки")
                sub: root.txt("audioRetentionCaption", "Расшифровка остаётся. Если расшифровать не удалось, звук не удаляется")
                rowEnabled: root.editable("meeting_audio_retention_days")
                AvSelect {
                    objectName: "meetingAudioRetention"
                    enabled: root.editable("meeting_audio_retention_days")
                    readonly property var days: {
                        var options = [0, 7, 30]
                        var selected = root.value("meeting_audio_retention_days", 0)
                        if (options.indexOf(selected) < 0) options.push(selected)
                        return options
                    }
                    model: days.map(function(day) {
                        if (day === 0) return "Не хранить"
                        var tail = day % 100
                        var unit = tail >= 11 && tail <= 14 ? "дней" : day % 10 === 1 ? "день" : day % 10 >= 2 && day % 10 <= 4 ? "дня" : "дней"
                        return String(day) + " " + unit
                    })
                    currentIndex: days.indexOf(root.value("meeting_audio_retention_days", 0))
                    onActivated: root.commit("meeting_audio_retention_days", days[index])
                }
            }
            SettingRow {
                width: parent.width
                label: root.txt("meetingRetention", "Удалять встречи старше")
                rowEnabled: root.editable("meeting_retention_days")
                AvSelect {
                    enabled: root.editable("meeting_retention_days")
                    model: ["Никогда", "30 дней", "90 дней", "365 дней"]
                    currentIndex: [0, 30, 90, 365].indexOf(root.value("meeting_retention_days", 0))
                    onActivated: root.commit("meeting_retention_days", [0, 30, 90, 365][index])
                }
            }
            SettingRow { width: parent.width; label: root.txt("privateCaption", "Встречи хранятся только на этом компьютере и доступны только вашей учётной записи") }
        }
        SettingGroup {
            width: parent.width
            title: root.txt("aiHeading", "ШАБЛОНЫ С ИИ")
            SettingRow {
                width: parent.width
                divider: false
                visible: root.bridge ? root.bridge.aiDisabled : false
                height: visible ? implicitHeight : 0
                label: root.txt("aiOff", "Шаблоны с ИИ отключены администратором")
                rowEnabled: false
            }
            Column {
                width: parent.width
                visible: root.bridge ? !root.bridge.aiDisabled : true
                Item {
                    width: parent.width
                    height: aiNote.implicitHeight + 20
                    NoteBanner { id: aiNote; x: Theme.cardRowPaddingX; y: 10; width: parent.width - x * 2; iconName: "info"; body: root.txt("aiDescription", "Краткое содержание и протокол встречи делает языковая модель на сервере, адрес которого вы укажете. Текст встречи уходит туда только по вашему нажатию. Пока адреса нет, этих шаблонов нет.") }
                }
                MeetingSettingField {
                    width: parent.width
                    label: root.txt("aiEndpoint", "Адрес сервиса ИИ")
                    value: root.value("meeting_ai_endpoint", "")
                    placeholder: "https://…/v1"
                    mono: true
                    maximumLength: 2048
                    rowEnabled: root.editable("meeting_ai_endpoint")
                    onCommitted: root.commit("meeting_ai_endpoint", value)
                }
                RowLayout {
                    x: Theme.cardRowPaddingX
                    width: parent.width - x * 2
                    visible: root.bridge ? root.bridge.endpointCaption !== "" : false
                    height: visible ? implicitHeight + Theme.meetingSectionAiClassMarginTop : 0
                    spacing: Theme.meetingSectionAiClassMarginTop
                    readonly property string kind: root.bridge && root.bridge.endpointKind !== undefined ? root.bridge.endpointKind : "muted"
                    readonly property color ink: kind === "loopback" || kind === "internal" ? Theme.successInk
                        : kind === "external" || kind === "external-admin" || kind === "plaintext" ? Theme.warningInk
                        : Theme.fgMuted
                    Icon {
                        name: parent.kind === "loopback" || parent.kind === "internal" ? "home" : "globe"
                        size: Theme.meetingSectionAiClassIcon
                        color: parent.ink
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.bridge ? root.bridge.endpointCaption : ""
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        color: parent.ink
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingSectionAiClassSize
                        renderType: Text.NativeRendering
                    }
                }
                Rectangle {
                    width: parent.width
                    visible: root.bridge ? root.bridge.aiAdminAddress : false
                    height: visible ? adminLine.implicitHeight + Theme.meetingSectionAdminRowPaddingY * 2 : 0
                    color: Theme.bgSurface2
                    RowLayout {
                        id: adminLine
                        x: Theme.meetingSectionAdminRowPaddingX
                        y: Theme.meetingSectionAdminRowPaddingY
                        width: parent.width - x * 2
                        spacing: Theme.meetingSectionAiClassMarginTop
                        Icon { name: "info"; size: Theme.meetingSectionAiClassIcon; color: Theme.fgMuted }
                        Text {
                            Layout.fillWidth: true
                            text: root.txt("aiAdminAddress", "Адрес задан администратором, модель и ключ — ваши")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: Theme.fgMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.meetingSectionAiClassSize
                            renderType: Text.NativeRendering
                        }
                    }
                }
                MeetingSettingField {
                    width: parent.width
                    stacked: false
                    label: root.txt("aiModel", "Модель")
                    value: root.value("meeting_ai_model", "")
                    maximumLength: 128
                    rowEnabled: root.editable("meeting_ai_model")
                    onCommitted: root.commit("meeting_ai_model", value)
                }
                MeetingSettingField {
                    width: parent.width
                    stacked: false
                    label: root.txt("aiKey", "Ключ доступа")
                    sub: root.txt("aiKeyCaption", "Хранится в системном хранилище паролей")
                    placeholder: root.txt("aiKeyOptional", "необязательно")
                    password: true
                    // Local field only. Secure credential persistence is M9.
                }
                SettingRow {
                    width: parent.width
                    sub: root.txt("aiCheckCaption", "Отправит пробный запрос без текста встреч")
                    AvButton { text: root.txt("aiCheck", "Проверить"); enabled: false }
                }
            }
        }
        RowLayout {
            width: parent.width
            spacing: Theme.meetingSectionOffBlockGap
            AvButton {
                objectName: "meetingDisable"
                text: root.txt("disable", "Выключить запись встреч")
                enabled: root.bridge ? !root.bridge.recording : false
                onClicked: if (root.bridge) root.bridge.disableMeetings()
            }
            Text {
                Layout.fillWidth: true
                text: root.bridge && root.bridge.recording ? root.txt("disableRecordingCaption", "Сначала остановите запись встречи") : root.txt("disableCaption", "Сохранённые встречи останутся. Удалить их можно в списке выше")
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                renderType: Text.NativeRendering
            }
        }
    }

    ResponsibilityDialog {
        parent: root.Window.window ? root.Window.window.contentItem : root
        bridge: root.bridge
    }
}
