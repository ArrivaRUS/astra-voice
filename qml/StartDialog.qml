// E13 M2 W4: design/meetings/spec.md §3.4/4.4/5/7; no capture on opening.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import "."
import "components"

ApplicationWindow {
    id: root
    objectName: "meetingStartWindow"
    property var meeting: typeof meetingBridge !== "undefined" ? meetingBridge : null
    property var settings: typeof settingsBridge !== "undefined" ? settingsBridge : null
    readonly property var copy: meeting && meeting.startTexts !== undefined ? meeting.startTexts : ({})
    readonly property var options: meeting && meeting.startOptions !== undefined ? meeting.startOptions : []
    readonly property var warnings: meeting && meeting.startWarnings !== undefined ? meeting.startWarnings : []
    readonly property string state: meeting && meeting.startState !== undefined ? meeting.startState : "default"
    readonly property bool starting: state === "starting"
    readonly property string error: meeting && meeting.startError !== undefined ? meeting.startError : ""
    property bool initialFocusPending: false
    readonly property var devices: settings && settings.devices && settings.devices.length > 0
        ? settings.devices : [{ id: "", name: qsTr("Системный по умолчанию") }]
    readonly property int selectedDeviceIndex: {
        var selected = settings ? settings.device : ""
        for (var i = 0; i < devices.length; ++i)
            if (devices[i].id === selected) return i
        return -1
    }
    function text(key, fallback) { return copy[key] !== undefined ? copy[key] : fallback }
    function deviceNames() {
        var names = []
        for (var i = 0; i < devices.length; ++i) names.push(devices[i].label || devices[i].name)
        return names
    }
    function cancel() { if (meeting) meeting.closeStartDialog() }
    function focusInitialButton() {
        if (!visible || !active || !initialFocusPending) return
        initialFocusPending = false
        startButton.forceActiveFocus(Qt.TabFocusReason)
    }
    function chooseNext(delta) {
        if (starting || !meeting || options.length === 0) return
        var current = -1
        for (var i = 0; i < options.length; ++i)
            if (options[i].id === meeting.selectedSource) current = i
        for (var step = 1; step <= options.length; ++step) {
            var next = (current + delta * step + options.length * 2) % options.length
            if (options[next].enabled !== false) {
                meeting.selectRecordingSource(options[next].id)
                Qt.callLater(ensureSelectedOptionVisible)
                return
            }
        }
    }
    function ensureSelectedOptionVisible() {
        if (optionScroll.height <= 0) return
        for (var i = 0; i < optionRepeater.count; ++i) {
            var option = optionRepeater.itemAt(i)
            if (!option || !option.selected) continue
            var top = option.y
            var bottom = top + option.height
            var position = optionScroll.contentY
            if (bottom > position + optionScroll.height) position = bottom - optionScroll.height
            // If a card is taller than the viewport, keep its title visible.
            if (top < position || option.height > optionScroll.height) position = top
            optionScroll.contentY = Math.max(0, Math.min(
                Math.max(0, optionScroll.contentHeight - optionScroll.height), position))
            return
        }
    }
    function handleKey(event) {
        if (event.key === Qt.Key_Escape) { cancel(); event.accepted = true }
        else if (event.key === Qt.Key_Down || event.key === Qt.Key_Right) { chooseNext(1); event.accepted = true }
        else if (event.key === Qt.Key_Up || event.key === Qt.Key_Left) { chooseNext(-1); event.accepted = true }
    }
    width: Theme.meetingStartW
    minimumWidth: Theme.meetingStartW
    maximumWidth: Theme.meetingStartW
    height: Math.min(Theme.meetingStartMaxH, footer.y + footer.height + Theme.meetingStartFooterPaddingBottom)
    maximumHeight: Theme.meetingStartMaxH
    title: text("title", qsTr("Записать встречу"))
    color: Theme.bgApp
    font.family: Theme.fontUi
    visible: meeting && meeting.recordingDevEnabled === true && meeting.startDialogOpen === true
    modality: Qt.WindowModal
    onClosing: function(close) { close.accepted = false; cancel() }
    onVisibleChanged: if (visible) {
        initialFocusPending = true
        requestActivate()
        Qt.callLater(focusInitialButton)
    }
    onActiveChanged: if (active && initialFocusPending) Qt.callLater(focusInitialButton)

    FocusScope {
        anchors.fill: parent
        focus: true
        Keys.onPressed: root.handleKey(event)
        Column {
            id: body
            x: Theme.meetingStartBodyPaddingX
            y: Theme.meetingStartBodyPaddingTop
            width: parent.width - x * 2
            spacing: Theme.meetingStartBodyGap
            Text {
                id: heading
                width: parent.width
                text: root.text("heading", qsTr("Что записывать"))
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontH3SubsectionSize
                font.weight: Font.Medium
                color: Theme.fg
                renderType: Text.NativeRendering
            }
            Flickable {
                id: optionScroll
                objectName: "meetingStartOptionsScroll"
                width: parent.width
                height: Math.ceil(Math.min(optionColumn.implicitHeight + Theme.meetingStartOptionBorder, Math.max(0,
                    Theme.meetingStartMaxH - Theme.meetingStartBodyPaddingTop - heading.height
                    - extras.implicitHeight - Theme.meetingStartBodyGap * 2
                    - Theme.meetingStartFooterPaddingTop - footer.height - Theme.meetingStartFooterPaddingBottom)))
                contentWidth: width
                contentHeight: optionColumn.implicitHeight + Theme.meetingStartOptionBorder
                onHeightChanged: Qt.callLater(root.ensureSelectedOptionVisible)
                onContentHeightChanged: Qt.callLater(root.ensureSelectedOptionVisible)
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                Column {
                    id: optionColumn
                    width: parent.width
                    spacing: Theme.meetingStartBodyGap
                    Repeater {
                        id: optionRepeater
                        model: root.options
                        delegate: AbstractButton {
                            id: optionButton
                            objectName: "meetingStartOption"
                            readonly property var option: modelData
                            readonly property bool selected: root.meeting ? root.meeting.selectedSource === option.id : false
                            onSelectedChanged: if (selected) Qt.callLater(root.ensureSelectedOptionVisible)
                            width: optionColumn.width
                            height: Math.max(optionText.implicitHeight, Theme.radioSize) + Theme.meetingStartOptionPaddingY * 2
                            enabled: !root.starting && option.enabled !== false
                            focusPolicy: Qt.StrongFocus
                            Accessible.role: Accessible.RadioButton
                            Accessible.name: option.title + ". " + option.caption
                            Accessible.checked: selected
                            onClicked: if (root.meeting) root.meeting.selectRecordingSource(option.id)
                            background: Rectangle {
                                color: Theme.meetingStartOptionBg
                                radius: Theme.meetingStartOptionRadius
                                Rectangle {
                                    anchors.fill: parent
                                    radius: parent.radius
                                    color: optionButton.selected ? Theme.meetingStartOptionSelectedBg : "transparent"
                                    border.width: optionButton.selected ? Theme.meetingStartOptionBorderSelected : Theme.meetingStartOptionBorder
                                    border.color: optionButton.selected ? Theme.meetingStartOptionSelectedBorder : Theme.border
                                }
                                Rectangle {
                                    anchors.fill: parent
                                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                                    radius: parent.radius + Theme.focusOffset
                                    color: "transparent"
                                    border.color: Theme.stateFocusRing
                                    border.width: Theme.focusWidth
                                    visible: optionButton.visualFocus
                                }
                            }
                            contentItem: Item {
                                Rectangle {
                                    x: Theme.meetingStartOptionPaddingX
                                    y: Theme.meetingStartOptionPaddingY + 3
                                    width: Theme.radioSize
                                    height: width
                                    radius: width / 2
                                    color: Theme.bgSurface
                                    border.width: Theme.radioBorder
                                    border.color: optionButton.selected ? Theme.radioOnBorder : Theme.radioBorderColor
                                    Rectangle {
                                        anchors.fill: parent
                                        anchors.margins: Theme.radioDotInset + Theme.radioBorder
                                        radius: width / 2
                                        visible: optionButton.selected
                                        color: Theme.radioDot
                                    }
                                }
                                Icon {
                                    id: optionIcon
                                    x: Theme.meetingStartOptionPaddingX + Theme.radioSize + Theme.meetingStartOptionGap
                                    y: Theme.meetingStartOptionPaddingY + 2.5
                                    name: optionButton.option.id === "room" ? "people" : optionButton.option.id === "all" ? "speaker" : "meeting"
                                    size: Theme.meetingStartOptionIcon
                                    color: Theme.fgMuted
                                }
                                Column {
                                    id: optionText
                                    x: optionIcon.x + optionIcon.width + Theme.meetingStartOptionGap
                                    y: Theme.meetingStartOptionPaddingY
                                    width: parent.width - x - Theme.meetingStartOptionPaddingX
                                    spacing: 2
                                    Text {
                                        width: parent.width
                                        text: optionButton.option.title
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        font.family: Theme.fontUi
                                        font.pixelSize: Theme.meetingStartOptionTitleSize
                                        color: optionButton.enabled ? Theme.fg : Theme.fgDisabled
                                        lineHeightMode: Text.FixedHeight
                                        lineHeight: Theme.meetingStartOptionTitleSize * 1.5
                                        renderType: Text.NativeRendering
                                    }
                                    Text {
                                        width: parent.width
                                        text: optionButton.option.caption
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        font.family: Theme.fontUi
                                        font.pixelSize: Theme.meetingStartOptionSubSize
                                        lineHeightMode: Text.FixedHeight
                                        lineHeight: Theme.meetingStartOptionSubSize * Theme.meetingStartOptionSubLineHeight
                                        color: optionButton.enabled ? Theme.fgMuted : Theme.fgDisabled
                                        renderType: Text.NativeRendering
                                    }
                                    Row {
                                        visible: optionButton.option.warning !== ""
                                        height: visible ? Math.max(noSplitText.implicitHeight, Theme.meetingStartOptionWarnIcon) + 3 : 0
                                        width: parent.width
                                        spacing: 4
                                        topPadding: 3
                                        Icon { name: "alert"; size: Theme.meetingStartOptionWarnIcon; color: Theme.warningInk }
                                        Text {
                                            id: noSplitText
                                            width: parent.width - Theme.meetingStartOptionWarnIcon - parent.spacing
                                            text: optionButton.option.warning
                                            textFormat: Text.PlainText
                                            wrapMode: Text.WordWrap
                                            font.family: Theme.fontUi
                                            font.pixelSize: Theme.meetingStartOptionWarnSize
                                            lineHeightMode: Text.FixedHeight
                                            lineHeight: Theme.meetingStartOptionSubSize * Theme.meetingStartOptionSubLineHeight
                                            color: Theme.warningInk
                                            renderType: Text.NativeRendering
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Column {
                id: extras
                width: parent.width
                spacing: Theme.meetingStartBodyGap
                RowLayout {
                    width: parent.width
                    height: microphoneSelector.implicitHeight + 2
                    spacing: Theme.meetingStartMicRowGap
                    Text {
                        id: microphoneLabel
                        Layout.fillWidth: true
                        text: root.text("microphone", qsTr("Микрофон"))
                        textFormat: Text.PlainText
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.meetingStartOptionTitleSize
                        color: Theme.fg
                        renderType: Text.NativeRendering
                    }
                    AvSelect {
                        id: microphoneSelector
                        objectName: "meetingStartMicrophone"
                        Layout.maximumWidth: parent.width - microphoneLabel.implicitWidth - Theme.meetingStartMicRowGap
                        enabled: !root.starting && (!root.settings || !root.settings.isLocked("device"))
                        model: root.deviceNames()
                        itemDetails: root.devices
                        popupMaxWidth: root.width - Theme.meetingStartBodyPaddingX * 2
                        onModelChanged: Qt.callLater(function() { microphoneSelector.currentIndex = root.selectedDeviceIndex })
                        Binding { target: microphoneSelector; property: "currentIndex"; value: root.selectedDeviceIndex }
                        onActivated: if (root.settings && currentIndex >= 0 && currentIndex < root.devices.length)
                            root.settings.device = root.devices[currentIndex].id
                    }
                }
                NoteBanner {
                    objectName: "meetingStartSpeakers"
                    width: parent.width
                    visible: root.meeting && root.meeting.startSpeakers === true
                    variant: "warn"
                    iconName: "head"
                    body: root.text("speakers", "")
                }
                Repeater {
                    model: root.warnings
                    delegate: NoteBanner {
                        objectName: "meetingStartWarning"
                        width: extras.width
                        variant: "warn"
                        iconName: "alert"
                        body: modelData
                    }
                }
                NoteBanner {
                    id: errorBanner
                    objectName: "meetingStartError"
                    width: parent.width
                    visible: root.error !== ""
                    variant: "error"
                    iconName: "alert"
                    body: root.error
                    AvButton {
                        visible: root.state === "error-disk" || root.state === "error-audio"
                        small: true
                        text: root.text(root.state === "error-disk" ? "openMeetings" : "retry", "")
                        onClicked: if (root.meeting) {
                            if (root.state === "error-disk") root.meeting.openInterruptedMeetings()
                            else root.meeting.retryStart()
                        }
                    }
                }
                Text {
                    width: parent.width
                    text: root.text("reminder", "")
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.meetingStartReminderSize
                    color: Theme.fgMuted
                    lineHeightMode: Text.FixedHeight
                    lineHeight: Theme.meetingStartReminderSize * 1.5
                    renderType: Text.NativeRendering
                }
            }
        }
        RowLayout {
            id: footer
            x: Theme.meetingStartFooterPaddingX
            y: body.y + body.implicitHeight + Theme.meetingStartFooterPaddingTop
            width: parent.width - x * 2
            height: implicitHeight
            spacing: 8
            Item { Layout.fillWidth: true }
            AvButton {
                objectName: "meetingStartCancel"
                text: root.text("cancel", qsTr("Отмена"))
                onClicked: root.cancel()
            }
            AvButton {
                id: startButton
                objectName: "meetingStartConfirm"
                text: root.text(root.starting ? "starting" : "start", "")
                iconName: root.starting ? "" : "rec"
                iconFill: true
                variant: "primary"
                implicitWidth: startContent.implicitWidth + leftPadding + rightPadding
                enabled: root.starting || (root.meeting && root.meeting.startCanStart === true)
                hoverEnabled: !root.starting
                onClicked: if (!root.starting && root.meeting && root.meeting.startCanStart === true)
                    root.meeting.on_start_clicked()
                Keys.onReturnPressed: if (!root.starting && enabled && root.meeting)
                    root.meeting.on_start_clicked()
                Keys.onEnterPressed: if (!root.starting && enabled && root.meeting)
                    root.meeting.on_start_clicked()
                contentItem: RowLayout {
                    id: startContent
                    spacing: Theme.buttonGap
                    BusyIndicator {
                        padding: 0
                        implicitWidth: Theme.buttonIconSize
                        implicitHeight: Theme.buttonIconSize
                        Layout.preferredWidth: Theme.buttonIconSize
                        Layout.preferredHeight: Theme.buttonIconSize
                        running: root.starting
                        visible: running
                        contentItem: Canvas {
                            id: spinnerCanvas
                            property color ink: startButton.fgColor
                            onInkChanged: requestPaint()
                            onWidthChanged: requestPaint()
                            onHeightChanged: requestPaint()
                            onPaint: {
                                if (width <= 1.5 || height <= 1.5) return
                                var context = getContext("2d")
                                context.clearRect(0, 0, width, height)
                                context.strokeStyle = ink
                                context.lineWidth = 1.5
                                context.lineCap = "round"
                                context.beginPath()
                                context.arc(width / 2, height / 2, (Math.min(width, height) - 1.5) / 2,
                                            -Math.PI / 2, Math.PI)
                                context.stroke()
                            }
                            RotationAnimator on rotation {
                                from: 0
                                to: 360
                                duration: 1000
                                loops: Animation.Infinite
                                running: root.starting && root.visible
                            }
                        }
                    }
                    Icon {
                        name: "rec"
                        fill: true
                        size: Theme.buttonIconSize
                        color: startButton.fgColor
                        visible: !root.starting
                    }
                    Text {
                        text: startButton.text
                        font: startButton.font
                        textFormat: Text.PlainText
                        color: startButton.fgColor
                        renderType: Text.NativeRendering
                    }
                }
            }
        }
    }
}
