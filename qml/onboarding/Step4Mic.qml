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

    readonly property var devices: microphoneDevices()
    readonly property string device: bridge ? bridge.device : ""
    readonly property real level: bridge ? bridge.level : 0
    readonly property string peak: bridge ? bridge.peak : ""
    readonly property string testDuration: bridge ? bridge.testDuration : ""
    readonly property string testModel: bridge && bridge.modelName ? bridge.modelName : qsTr("GigaAM v3 RNN-T")
    readonly property string testPhrase: bridge ? bridge.testPhrase : ""
    readonly property string testText: bridge ? bridge.testText : ""
    readonly property string testState: bridge ? bridge.testState : "idle"
    readonly property string testMessage: bridge ? bridge.testMessage : ""
    readonly property bool silent: level <= 0.02 // Порог «тишина» по заданию шага 4.
    readonly property bool testing: testState === "preparing" || testState === "recording" || testState === "processing"
    readonly property bool deviceMissing: device !== "" && deviceIndex(device) === -1
    readonly property int selectedDeviceIndex: deviceIndex(device) >= 0 ? deviceIndex(device)
        : deviceIndex("") >= 0 ? deviceIndex("") : 0

    // Без моста или устройств оставляем системный выбор с пустым идентификатором.
    function microphoneDevices() {
        if (root.bridge && root.bridge.devices.length > 0)
            return root.bridge.devices
        return [{ id: "", name: qsTr("Системный по умолчанию") }]
    }

    // Список показывает имена; идентификаторы остаются в исходных объектах.
    function deviceNames() {
        var names = []
        for (var i = 0; i < root.devices.length; ++i)
            names.push(root.devices[i].name)
        return names
    }

    // Выбор ищем по идентификатору: одинаковые имена не объединяют устройства.
    function deviceIndex(deviceId) {
        for (var i = 0; i < root.devices.length; ++i) {
            if (root.devices[i].id === deviceId)
                return i
        }
        return -1
    }

    // Обратно в мост передаём идентификатор выбранного пункта, а не его имя.
    function deviceIdAt(index) {
        return index >= 0 && index < root.devices.length ? root.devices[index].id : ""
    }

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
        antialiasing: true
        clip: true

        Column {
            id: rows
            x: Theme.cardBorder
            y: Theme.cardBorder
            width: parent.width - Theme.cardBorder * 2

            SettingRow {
                id: deviceRow
                width: parent.width
                divider: false
                showHint: false
                label: qsTr("Микрофон")
                // Для системного выбора поясняем, какой микрофон фактически используется.
                sub: root.device === "" && root.bridge ? root.bridge.deviceResolved : ""

                AvSelect {
                    id: deviceSelector
                    Layout.preferredWidth: 236 // Макет: ширина списка микрофонов.
                    Layout.alignment: Qt.AlignVCenter
                    model: root.deviceNames()

                    // Binding сохраняет синхронизацию после ручного выбора.
                    Binding {
                        target: deviceSelector
                        property: "currentIndex"
                        value: root.selectedDeviceIndex
                    }

                    onActivated: {
                        if (root.bridge)
                            root.bridge.device = root.deviceIdAt(currentIndex)
                    }
                }
            }

            Text {
                x: Theme.cardRowPaddingX
                width: parent.width - Theme.cardRowPaddingX * 2
                visible: root.deviceMissing
                height: visible ? implicitHeight : 0
                bottomPadding: Theme.cardRowPaddingY
                text: qsTr("Выбранный раньше микрофон не найден — включён системный по умолчанию")
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSettingSubSize
                lineHeight: Math.round(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
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
                    textFormat: Text.PlainText
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
                    textFormat: Text.PlainText
                    text: root.testState === "preparing" ? root.testMessage
                        : root.testState === "idle" ? qsTr("Нажмите «Тестовая диктовка», чтобы проверить микрофон")
                        : root.testState === "recording" && root.silent ? qsTr("Звука с этого микрофона пока нет")
                        : root.peak !== "" ? qsTr("Пик %1 · уровень в норме").arg(root.peak) : ""
                    visible: text !== ""
                    height: visible ? implicitHeight : 0
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
                text: root.testing ? qsTr("Остановить") : qsTr("Тестовая диктовка")
                enabled: true
                onClicked: {
                    if (root.bridge) {
                        if (root.testing)
                            root.bridge.stopTest()
                        else
                            root.bridge.startTest()
                    }
                }
            }
        }
    }

    Rectangle {
        id: resultField
        visible: root.testText !== "" || root.testState === "recording" || root.testState === "processing"
        y: levelBox.y + levelBox.height + (visible ? 10 : 0) // Макет: margin-top .field.
        width: root.width
        // design/spec.md §4.5: текст, паддинги и обе границы дают 35,5 → 36 px для одной строки.
        height: visible ? Math.ceil(resultText.height + Theme.fieldPaddingY * 2 + Theme.fieldBorder * 2) : 0
        radius: Theme.fieldRadius
        color: Theme.bgSurface
        antialiasing: true

        Text {
            id: resultText
            x: Theme.fieldPaddingX
            // FixedHeight оставляет leading снизу; CSS делит его пополам.
            // Паддинг отсчитываем от внутренней стороны рамки, как в макете.
            y: Theme.fieldBorder + Theme.fieldPaddingY + (lineHeight - resultFontMetrics.height) / 2
            width: parent.width - Theme.fieldPaddingX * 2
            text: root.testState === "recording"
                ? (root.testPhrase !== "" ? qsTr("Слушаю… Скажите: «%1»").arg(root.testPhrase) : qsTr("Слушаю…"))
                : root.testState === "processing" ? qsTr("Распознаю…") : root.testText
            color: root.testState === "recording" || root.testState === "processing" ? Theme.fgMuted : Theme.fg
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontFieldSize
            // Макет 08-onboarding-4-mic.html (.field): базовый межстрочный интервал 1,5.
            lineHeight: Theme.fontFieldSize * 1.5
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }

        FontMetrics {
            id: resultFontMetrics
            font: resultText.font
        }

        // Обводка рисуется поверх заливки: в тёмной теме Theme.border — 10 % белого.
        // Композит считаем от заливки, иначе фон окна делает рамку темнее макета.
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: "transparent"
            border.width: Theme.fieldBorder
            border.color: Theme.border
            antialiasing: true
        }
    }

    RowLayout {
        id: outcome
        visible: (root.testState === "done" && root.testText !== "") || root.testState === "error"
        y: resultField.y + resultField.height + (visible ? 7 : 0) // Макет: margin-top .c12.
        width: root.width
        height: visible ? implicitHeight : 0
        spacing: 6 // Макет: зазор 6 px между иконкой и подписью итога.

        Icon {
            name: root.testState === "error" ? "alert" : "check"
            size: 12 // Макет: иконка строки итога.
            color: root.testState === "error" ? Theme.dangerInk : Theme.successInk
            // Низ inline-иконки стоит на базовой линии подписи, как в CSS макета.
            baselineOffset: height
            Layout.alignment: Qt.AlignBaseline
        }

        Text {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignBaseline
            textFormat: Text.PlainText
            text: root.testState === "error"
                ? (root.testMessage !== "" ? root.testMessage : qsTr("Не удалось распознать — попробуйте ещё раз"))
                : root.testDuration !== ""
                    ? qsTr("Распознано за %1 · модель %2 · текст никуда не вставлен").arg(root.testDuration).arg(root.testModel)
                    : qsTr("Распознано · модель %1 · текст никуда не вставлен").arg(root.testModel)
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
        visible: root.testState === "recording" && root.silent
        y: outcome.y + outcome.height + (visible ? 11 : 0) // Макет: margin-top .note.w.
        width: root.width
        height: visible ? implicitHeight : 0
        variant: "warn"
        iconName: "alert"
        title: qsTr("Микрофон молчит")
        body: qsTr("Устройство открыто, но звука нет. Выберите другой микрофон или проверьте громкость записи в настройках звука.")
    }
}
