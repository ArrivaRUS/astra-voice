// Общая панель 232 / 300 — spec §3.7, tokens §3.4–3.7.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Rectangle {
    id: root
    objectName: "meetingCardParticipants"
    property var bridge: null
    property bool drawer: false
    property Item tableFocusTarget: null
    property string selectedSpeaker: ""
    property string highlightedSpeaker: ""
    property string operationError: ""
    readonly property var card: bridge && bridge.card !== undefined ? bridge.card : ({})
    readonly property var participants: card.participants || []
    readonly property var texts: bridge && bridge.speakerTexts !== undefined ? bridge.speakerTexts : ({})
    readonly property var cardTexts: bridge && bridge.cardTexts !== undefined ? bridge.cardTexts : ({})
    property alias mergeButton: mergeButton
    signal closeRequested()
    signal mergeRequested(var participant, var origin)

    function focusFirst() {
        for (var i = 0; i < people.count; ++i) {
            var row = people.itemAt(i)
            if (row && row.field.enabled && row.field.visible) {
                row.field.forceActiveFocus(Qt.TabFocusReason)
                return true
            }
        }
        if (drawer) {
            closeButton.forceActiveFocus(Qt.TabFocusReason)
            return true
        }
        return false
    }
    function restoreFields() {
        for (var i = 0; i < people.count; ++i) people.itemAt(i).restore()
    }
    function showMerge(speakerId) {
        selectedSpeaker = speakerId
        var source = mergeSource()
        if (source) mergeRequested(source, mergeButton)
    }
    function speakerAnchor(speakerId, target) {
        var left = root.mapToItem(target, 0, 0)
        for (var i = 0; i < people.count; ++i) {
            var row = people.itemAt(i)
            if (row && row.info.id === speakerId) {
                var point = row.mapToItem(target, 0, 0)
                return { x: left.x, y: point.y, height: row.height }
            }
        }
        return { x: left.x, y: left.y, height: 0 }
    }
    function mergeSource() {
        for (var i = 0; i < participants.length; ++i)
            if (participants[i].id === selectedSpeaker && participants[i].canMerge) return participants[i]
        return null
    }
    function txt(key) { return texts[key] || "" }
    Connections {
        target: root.bridge
        ignoreUnknownSignals: true
        function onSpeakerOperationFinished(code) {
            var result = root.bridge.speakerOperation
            if (!result.ok) {
                root.restoreFields()
                root.operationError = result.text
            } else root.operationError = ""
        }
    }
    width: drawer ? Theme.participantsDrawerW : Theme.participantsPanelW
    height: body.height + (drawer ? 0 : Theme.cardBorder * 2)
    color: drawer ? Theme.participantsDrawerBg : Theme.bgSurface
    radius: drawer ? 0 : Theme.cardRadius
    border.width: drawer ? 0 : Theme.cardBorder
    border.color: Qt.tint(Theme.bgSurface, Theme.border)

    Column {
        id: body
        x: root.drawer ? 0 : Theme.cardBorder
        y: x
        width: parent.width - x * 2
        Item {
            width: parent.width
            height: Math.max(heading.height, closeButton.visible ? closeButton.height : 0) + Theme.participantsHeadPaddingY * 2
            Text {
                id: heading
                x: Theme.participantsHeadPaddingX
                anchors.verticalCenter: parent.verticalCenter
                text: root.cardTexts.participants || ""
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.participantsTitleSize
                font.weight: Font.Medium
                color: Theme.fg
                height: Theme.participantsTitleSize * Theme.fontBodyLineHeight
                verticalAlignment: Text.AlignVCenter
                renderType: Text.NativeRendering
            }
            Icon {
                visible: !root.drawer
                anchors.right: parent.right
                anchors.rightMargin: Theme.participantsHeadPaddingX
                anchors.verticalCenter: parent.verticalCenter
                name: "people"
                size: Theme.participantsHeadIcon
                color: Theme.fgMuted
            }
            AvButton {
                id: closeButton
                objectName: "meetingParticipantsClose"
                Keys.onReturnPressed: if (enabled) clicked()
                Keys.onEnterPressed: if (enabled) clicked()
                visible: root.drawer
                anchors.right: parent.right
                anchors.rightMargin: Theme.participantsHeadPaddingX
                anchors.verticalCenter: parent.verticalCenter
                small: true
                variant: "ghost"
                iconName: "x"
                Accessible.name: root.txt("closeNames")
                onClicked: root.closeRequested()
            }
        }
        Repeater {
            id: people
            // Число — модель; изменение имени не уничтожает поле и его фокус.
            model: root.participants.length
            Rectangle {
                id: person
                objectName: "meetingCardParticipant"
                readonly property var info: root.participants[index] || ({})
                property alias field: field
                function restore() { field.text = info.name || "" }
                width: body.width
                height: content.height + Theme.participantsItemPaddingY * 2 + Theme.cardBorder
                color: root.highlightedSpeaker === info.id ? Theme.participantsHighlightBg : root.color
                Rectangle { width: parent.width; height: Theme.cardBorder; color: Theme.borderSoft }
                Column {
                    id: content
                    x: Theme.participantsItemPaddingX
                    y: Theme.cardBorder + Theme.participantsItemPaddingY
                    width: parent.width - x * 2
                    spacing: person.info.kind === "speaker" ? Theme.participantsItemGap : 0
                    Item {
                        width: parent.width
                        height: Theme.participantsNameSize * Theme.fontBodyLineHeight
                        MeetingSpeakerNumber {
                            id: number
                            num: person.info.num || ""
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            id: label
                            objectName: "meetingParticipantLabel"
                            x: number.visible ? number.width + Theme.speakerNumGap : 0
                            width: Math.max(0, minutes.x - Theme.speakerNumGap - x)
                            height: parent.height
                            text: person.info.kind === "speaker" ? (person.info.panelLabel || "") : (person.info.label || "")
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                            color: person.info.kind === "me" ? Theme.transcriptMeFg : Theme.fg
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.participantsNameSize
                            font.weight: Font.Medium
                            renderType: Text.NativeRendering
                        }
                        Text {
                            id: minutes
                            objectName: "meetingCardMinutes"
                            x: parent.width - width
                            y: label.baselineOffset - baselineOffset
                            text: person.info.minutes || ""
                            textFormat: Text.PlainText
                            color: Theme.fgMuted
                            font.family: Theme.fontMono
                            font.pixelSize: Theme.participantsMinutesSize
                            font.letterSpacing: Theme.participantsMinutesSize * Theme.fontMonoInlineTracking
                            renderType: Text.NativeRendering
                        }
                    }
                    // Qt5 rounds font.pixelSize to int. Render at ceil(size), then
                    // map both glyphs and layout back to the specified 11.5 logical px.
                    Item {
                        visible: person.info.kind === "me" || !!person.info.firstText
                        width: parent.width
                        objectName: "meetingParticipantQuoteBox"
                        height: person.info.kind === "me" ? Math.ceil(quote.height * quote.scale)
                            : quote.height * quote.scale
                        Text {
                            id: quote
                            objectName: "meetingParticipantQuote"
                            transformOrigin: Item.TopLeft
                            scale: Theme.participantsQuoteSize / Math.ceil(Theme.participantsQuoteSize)
                            width: parent.width / scale
                            text: person.info.kind === "me" ? (root.cardTexts.meCaption || "")
                                : person.info.firstText ? "«" + person.info.firstText + "»" : ""
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            maximumLineCount: person.info.kind === "speaker" ? 2 : Number.MAX_VALUE
                            elide: Text.ElideRight
                            color: Theme.fgMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Math.ceil(Theme.participantsQuoteSize)
                            lineHeight: Math.ceil(Theme.participantsQuoteSize) * Theme.participantsQuoteLineHeight
                            lineHeightMode: Text.FixedHeight
                            renderType: Text.NativeRendering
                        }
                    }
                    TextField {
                        id: field
                        objectName: "meetingSpeakerName"
                        readonly property string speakerId: person.info.id || ""
                        property string savedName: person.info.name || ""
                        onSavedNameChanged: if (!activeFocus) text = savedName
                        Component.onCompleted: text = savedName
                        visible: person.info.kind === "speaker"
                        enabled: root.card.namesEditable === true && person.info.canEdit === true
                        width: parent.width
                        height: Theme.participantsFieldSize * Theme.fontBodyLineHeight + Theme.participantsFieldPaddingY * 2 + Theme.fieldBorder * 2
                        text: savedName
                        placeholderText: root.txt("placeholder")
                        maximumLength: 60
                        selectByMouse: true
                        color: enabled ? Theme.fg : Theme.fgDisabled
                        placeholderTextColor: enabled ? Theme.fgMuted : Theme.fgDisabled
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.participantsFieldSize
                        renderType: Text.NativeRendering
                        leftPadding: Theme.participantsFieldPaddingX
                        rightPadding: Theme.participantsFieldPaddingX
                        topPadding: Theme.participantsFieldPaddingY
                        bottomPadding: Theme.participantsFieldPaddingY
                        Accessible.name: person.info.inputAccessibleName || ""
                        Accessible.description: root.card.namesHint || ""
                        onActiveFocusChanged: {
                            if (activeFocus) root.selectedSpeaker = speakerId
                            else text = savedName
                        }
                        Keys.onBacktabPressed: {
                            var first = root.participants.findIndex(function(p) { return p.canEdit })
                            if (!root.drawer && index === first && root.tableFocusTarget) root.tableFocusTarget.forceActiveFocus(Qt.BacktabFocusReason)
                            else event.accepted = false
                        }
                        onTextEdited: {
                            if (root.bridge && !root.bridge.renameSpeaker(speakerId, text)) {
                                person.restore()
                                root.operationError = root.bridge.speakerOperation.text
                            }
                        }
                        background: Rectangle {
                            color: field.enabled ? Theme.bgSurface : Theme.bgSurface2
                            radius: Theme.fieldRadius
                            Rectangle {
                                objectName: "meetingSpeakerFieldBorder"
                                anchors.fill: parent
                                color: "transparent"
                                radius: parent.radius
                                border.width: Theme.fieldBorder
                                border.color: Qt.tint(parent.color, Theme.border)
                            }
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                                color: "transparent"
                                radius: Theme.focusRadius
                                border.width: Theme.focusWidth
                                border.color: Theme.stateFocusRing
                                visible: field.activeFocus
                            }
                        }
                    }
                }
            }
        }
        Item {
            width: parent.width
            height: visible ? mergeButton.height + Theme.participantsFootPaddingY * 2 + Theme.cardBorder : 0
            visible: root.participants.some(function(p) { return p.kind === "speaker" }) && root.card.live !== true
            Rectangle { width: parent.width; height: Theme.cardBorder; color: Theme.borderSoft }
            AvButton {
                id: mergeButton
                objectName: "meetingMergeOpen"
                Keys.onReturnPressed: if (enabled) clicked()
                Keys.onEnterPressed: if (enabled) clicked()
                x: Theme.participantsFootPaddingX
                y: Theme.participantsFootPaddingY + Theme.cardBorder
                small: true
                variant: "ghost"
                iconName: "merge"
                text: root.txt("merge")
                enabled: root.card.namesEditable === true && root.participants.some(function(p) { return p.canMerge })
                onClicked: {
                    var source = root.mergeSource()
                    if (source) root.mergeRequested(source, mergeButton)
                    else sourceMenu.open()
                }
                Menu {
                    id: sourceMenu
                    y: mergeButton.height
                    width: Theme.popoverMinW
                    Repeater {
                        model: root.participants.filter(function(p) { return p.canMerge })
                        MenuItem {
                            text: modelData.label
                            contentItem: Text {
                                text: modelData.label
                                textFormat: Text.PlainText
                                color: Theme.fg
                                font.family: Theme.fontUi
                                font.pixelSize: Theme.fontMenuItemSize
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                                renderType: Text.NativeRendering
                            }
                            onTriggered: {
                                root.selectedSpeaker = modelData.id
                                root.mergeRequested(modelData, mergeButton)
                            }
                        }
                    }
                }
            }
        }
        Text {
            objectName: "meetingCardNamesLater"
            visible: root.card.live === true || root.participants.some(function(p) { return p.kind === "speaker" })
            x: Theme.participantsCaptionPaddingX
            width: parent.width - x * 2
            topPadding: root.card.live === true ? Theme.participantsFootPaddingY : Theme.participantsCaptionPaddingTop
            bottomPadding: Theme.participantsCaptionPaddingBottom
            text: root.card.namesHint || ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.participantsCaptionSize
            lineHeight: Theme.participantsCaptionSize * Theme.fontBodyLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
        Text {
            objectName: "meetingSpeakerError"
            visible: text !== ""
            x: Theme.participantsCaptionPaddingX
            width: parent.width - x * 2
            bottomPadding: Theme.participantsCaptionPaddingBottom
            text: root.operationError
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: Theme.dangerInk
            font.family: Theme.fontUi
            font.pixelSize: Theme.participantsCaptionSize
            renderType: Text.NativeRendering
            Accessible.role: Accessible.AlertMessage
            Accessible.name: text
        }
    }
}
