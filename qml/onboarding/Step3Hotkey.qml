// design/spec.md §7, §10; design/mockups/final/08-onboarding-3-hotkey.html.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property string barHint: ""
    property bool skipEnabled: true

    readonly property string hotkey: bridge ? bridge.hotkey : qsTr("Ctrl + Space")
    readonly property string hotkeyMode: bridge ? bridge.hotkeyMode : "ptt"

    function escPressed() {
        return capture.escPressed()
    }

    implicitWidth: 580 // Макет 08-onboarding-3-hotkey.html: ширина содержимого.
    implicitHeight: capture.visible ? capture.y + capture.height : card.y + card.height
    width: implicitWidth
    height: implicitHeight

    Text {
        id: heading
        width: root.width
        text: qsTr("Горячая клавиша")
        color: Theme.fg
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontH2SectionSize
        font.weight: Font.Bold
        lineHeight: Theme.fontH2SectionSize * Theme.fontH2SectionLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    Text {
        id: subtitle
        y: heading.height + 4 // Макет 08-onboarding-3-hotkey.html: margin-top подзаголовка.
        width: root.width
        text: qsTr("Зажмите её и говорите. Отпустили — текст появится там, где стоит курсор.")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    Rectangle {
        id: card
        y: subtitle.y + subtitle.height + 14 // Макет 08-onboarding-3-hotkey.html: margin-bottom подзаголовка.
        width: root.width
        height: rows.height + Theme.cardBorder * 2
        color: Theme.bgSurface
        radius: Theme.cardRadius
        antialiasing: true
        clip: true

        Column {
            id: rows
            x: Theme.cardBorder
            y: Theme.cardBorder
            width: parent.width - Theme.cardBorder * 2

            SettingRow {
                width: parent.width
                divider: false
                showHint: false
                label: qsTr("Текущая комбинация")

                RowLayout {
                    spacing: 8 // design/spec.md §7: чип + «Изменить».
                    Layout.alignment: Qt.AlignVCenter

                    KeyChip {
                        text: root.hotkey
                        Layout.alignment: Qt.AlignVCenter
                    }

                    AvButton {
                        text: qsTr("Изменить")
                        small: true
                        Layout.alignment: Qt.AlignVCenter
                        onClicked: {
                            if (root.bridge && root.bridge.beginCapture)
                                root.bridge.beginCapture()
                        }
                    }
                }
            }

            SettingRow {
                width: parent.width
                showHint: false
                label: qsTr("Режим")
                sub: qsTr("Удерживать — самый предсказуемый вариант")

                AvSegmented {
                    id: modeSelector
                    options: [qsTr("Удерживать"), qsTr("Нажать-нажать")]
                    Layout.alignment: Qt.AlignVCenter

                    // Клики AvSegmented присваивают currentIndex. Binding сохраняет
                    // обратное обновление из bridge после ручного выбора режима.
                    Binding {
                        target: modeSelector
                        property: "currentIndex"
                        value: root.hotkeyMode === "toggle" ? 1 : 0
                    }

                    onCurrentIndexChanged: {
                        var mode = currentIndex === 1 ? "toggle" : "ptt"
                        if (root.bridge && root.bridge.hotkeyMode !== mode)
                            root.bridge.hotkeyMode = mode
                    }
                }
            }
        }

        // Обводка рисуется поверх заливки: в тёмной теме Theme.border — 10 % белого.
        // Композит считаем от заливки, иначе фон окна делает рамку темнее макета.
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: "transparent"
            border.width: Theme.cardBorder
            border.color: Theme.border
            antialiasing: true
        }
    }

    Text {
        id: captureHeading
        y: card.y + card.height + 16 // Макет 08-onboarding-3-hotkey.html: margin-top группы.
        width: root.width
        visible: capture.visible
        height: visible ? lineHeight : 0
        text: qsTr("Назначение новой комбинации")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontGroupCapsSize
        font.weight: Font.Medium
        font.capitalization: Font.AllUppercase
        font.letterSpacing: Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize
        leftPadding: 2 // design/spec.md §3.1: отступ CAPS-заголовка слева.
        lineHeight: Math.round(Theme.fontGroupCapsSize * Theme.fontGroupCapsLineHeight)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
    }

    CaptureField {
        id: capture
        y: captureHeading.y + captureHeading.height + 7 // Макет 08-onboarding-3-hotkey.html: margin-bottom группы.
        width: root.width
        visible: state7 !== "idle"
        height: visible ? implicitHeight : 0
        state7: root.bridge ? root.bridge.captureState : "capturing"
        hotkey: root.hotkey
        captureMessage: root.bridge ? root.bridge.captureMessage : ""
        pendingCombo: root.bridge ? root.bridge.pendingCombo : ""
        freeCandidates: root.bridge ? root.bridge.freeCandidates : []
        // Смежная зона: имя владельца конфликта пока отсутствует в контракте Python.
        conflictOwner: ""

        onState7Changed: {
            if ((state7 === "conflict" || state7 === "duplicate" || state7 === "not-grabbed")
                    && root.bridge && root.bridge.refreshCandidates)
                root.bridge.refreshCandidates()
        }

        onChangeRequested: {
            if (root.bridge && root.bridge.beginCapture)
                root.bridge.beginCapture()
        }
        onCancelRequested: {
            if (root.bridge && root.bridge.cancelCapture)
                root.bridge.cancelCapture()
        }
        onComboCaptured: {
            if (root.bridge && root.bridge.endCapture)
                root.bridge.endCapture(combo)
        }
        onChooseAnotherRequested: {
            if (root.bridge && root.bridge.beginCapture)
                root.bridge.beginCapture()
        }
        onRetryRequested: {
            if (root.bridge && root.bridge.beginCapture)
                root.bridge.beginCapture()
        }
        onKeepRequested: {
            if (root.bridge && root.bridge.keepCombo)
                root.bridge.keepCombo()
        }
        onToggleModeRequested: {
            if (root.bridge)
                root.bridge.hotkeyMode = "toggle"
        }
    }
}
