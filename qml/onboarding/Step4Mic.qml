// design/spec.md §4.5, §8.3, §10; design/mockups/final/08-onboarding-4-mic.html.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property string barHint: ""
    property bool skipEnabled: true

    readonly property string device: bridge ? bridge.device : ""
    readonly property real level: bridge ? bridge.level : 0
    readonly property string testText: bridge ? bridge.testText : ""
    readonly property string testState: bridge ? bridge.testState : ""
    readonly property bool silent: level <= 0.02 // Порог «тишина» по заданию шага 4.
    readonly property bool testing: testState === "recording" || testState === "processing"

    implicitWidth: 580 // Макет 08-onboarding-4-mic.html: ширина содержимого.
    implicitHeight: silenceNote.y + silenceNote.height
    width: implicitWidth
    height: implicitHeight

    Text {
        id: heading
        width: root.width
        text: qsTr("Проверим микрофон")
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
        y: heading.height + 4 // Макет 08-onboarding-4-mic.html: margin-top подзаголовка.
        width: root.width
        text: qsTr("Скажите любую фразу — мы покажем уровень и распознаем её прямо здесь. Никуда вставлять не будем.")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    // Карточка SettingGroup без пустого CAPS-заголовка и его отступов.
    Rectangle {
        id: card
        y: subtitle.y + subtitle.height + 14 // Макет: margin-bottom подзаголовка.
        width: root.width
        height: rows.height + Theme.cardBorder * 2
        color: Theme.bgSurface
        radius: Theme.cardRadius
        border.width: Theme.cardBorder
        border.color: Theme.border
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
                label: qsTr("Микрофон")
                sub: root.device

                AvSelect {
                    id: deviceSelector
                    Layout.preferredWidth: 236 // Макет: ширина списка микрофонов.
                    Layout.alignment: Qt.AlignVCenter
                    model: root.bridge && root.bridge.devices
                           ? root.bridge.devices : [qsTr("Системный по умолчанию")]

                    // Binding сохраняет синхронизацию после ручного выбора.
                    Binding {
                        target: deviceSelector
                        property: "currentIndex"
                        value: root.bridge ? deviceSelector.model.indexOf(root.device) : 0
                    }

                    onActivated: {
                        if (root.bridge)
                            root.bridge.device = currentText
                    }
                }
            }
        }
    }

    Rectangle {
        id: levelBox
        y: card.y + card.height + 12 // Макет: margin-top .lvbox.
        width: root.width
        height: levelRow.implicitHeight + Theme.micLevelMeterBoxPaddingY * 2
        color: Theme.micLevelMeterBoxBg
        radius: Theme.micLevelMeterBoxRadius
        antialiasing: true

        RowLayout {
            id: levelRow
            x: Theme.micLevelMeterBoxPaddingX
            y: Theme.micLevelMeterBoxPaddingY
            width: parent.width - Theme.micLevelMeterBoxPaddingX * 2
            spacing: 12 // Макет: gap внутри .lvbox.

            LevelMeter {
                level: root.level
                silent: root.silent
                Layout.alignment: Qt.AlignVCenter
            }

            Column {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 2 // Макет: margin-top .sub.

                Text {
                    width: parent.width
                    text: root.silent ? qsTr("Пока тишина") : qsTr("Слышим вас")
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingLabelSize
                    lineHeight: Theme.fontSettingLabelSize * Theme.fontSettingLabelLineHeight
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                    wrapMode: Text.WordWrap
                }

                Text {
                    width: parent.width
                    text: root.silent ? qsTr("Звука с этого микрофона пока нет") : qsTr("Уровень в норме")
                    color: Theme.fgMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingSubSize
                    lineHeight: Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                    wrapMode: Text.WordWrap
                }
            }

            AvButton {
                Layout.alignment: Qt.AlignVCenter
                iconName: "chip"
                text: qsTr("Сказать тестовую фразу")
                enabled: !root.testing
                onClicked: {
                    if (root.bridge)
                        root.bridge.testPhrase()
                }
            }
        }
    }

    Rectangle {
        id: resultField
        visible: root.testText !== "" || root.testing
        y: levelBox.y + levelBox.height + (visible ? 10 : 0) // Макет: margin-top .field.
        width: root.width
        height: visible ? resultText.height + Theme.fieldPaddingY * 2 : 0
        radius: Theme.fieldRadius
        border.width: Theme.fieldBorder
        border.color: Theme.border
        color: Theme.bgSurface
        antialiasing: true

        Text {
            id: resultText
            x: Theme.fieldPaddingX
            y: Theme.fieldPaddingY
            width: parent.width - Theme.fieldPaddingX * 2
            text: root.testState === "recording" ? qsTr("Слушаю…")
                : root.testState === "processing" ? qsTr("Распознаю…") : root.testText
            color: root.testing ? Theme.fgMuted : Theme.fg
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontFieldSize
            renderType: Text.NativeRendering
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
    }

    RowLayout {
        id: outcome
        visible: (root.testState === "done" && root.testText !== "") || root.testState === "error"
        y: resultField.y + resultField.height + (visible ? 7 : 0) // Макет: margin-top .c12.
        width: root.width
        height: visible ? implicitHeight : 0
        spacing: 4 // Макет: пробел между иконкой и подписью итога.

        Icon {
            name: root.testState === "error" ? "alert" : "check"
            size: 12 // Макет: иконка строки итога.
            color: root.testState === "error" ? Theme.dangerInk : Theme.successInk
            Layout.alignment: Qt.AlignVCenter
        }

        Text {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            text: root.testState === "error" ? qsTr("Не удалось распознать — попробуйте ещё раз")
                : qsTr("Распознано — текст никуда не вставлен")
            color: root.testState === "error" ? Theme.dangerInk : Theme.successInk
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaptionSize
            lineHeight: Theme.fontCaptionSize * Theme.fontCaptionLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
        }
    }

    NoteBanner {
        id: silenceNote
        visible: root.silent
        y: outcome.y + outcome.height + (visible ? 11 : 0) // Макет: margin-top .note.w.
        width: root.width
        height: visible ? implicitHeight : 0
        variant: "warn"
        iconName: "alert"
        title: qsTr("Микрофон молчит")
        body: qsTr("Устройство открыто, но звука нет. Выберите другой микрофон или проверьте громкость записи в настройках звука.")
    }
}
