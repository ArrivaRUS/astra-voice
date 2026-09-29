// Панель «Что нового» — design/spec.md §6.1; референс design/refs/04-update-panel.png (+ -dark).
// M7-ядро v0.2: состояния без установки — checking · uptodate · available · unavailable ·
// skipped. Установка, скачивание пакета и «Обновить из файла…» — следующие шаги (M8).
//
// Текст «Что нового» пришёл из сети и НЕ доверенный: только Text.PlainText (не AutoText —
// mightBeRichText отрисует HTML, а <img src="http…"> загрузит картинку мимо NetworkGate).
// Ссылок в тексте нет: страницу выпуска открывает только кнопка через мост.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Rectangle {
    id: root

    // checking | uptodate | available | unavailable | skipped
    property string panelState: "available"
    property string version: ""
    // Строки «Что нового» от моста: [{ text, bullet }] — недоверенный простой текст.
    property var notes: []
    property string currentVersion: ""
    property string checkedText: ""
    property bool releasePageAvailable: false
    property bool canCheckNow: false
    property bool autoCheck: false

    signal releasePageRequested()
    signal skipRequested()
    signal remindLaterRequested()
    signal checkRequested()
    signal showSkippedRequested()

    readonly property bool accent: panelState === "available"

    // Отступы 14 / 16 — внутри рамки 1 px (макет считает по border-box).
    implicitHeight: body.implicitHeight + (Theme.updatePanelPaddingY + Theme.cardBorder) * 2
    height: implicitHeight
    radius: Theme.updatePanelRadius
    color: Theme.bgSurface
    antialiasing: true

    // Граница 1 px: primary — есть что ставить, border — нейтральные состояния (§6.1).
    Rectangle {
        anchors.fill: parent
        radius: parent.radius
        color: "transparent"
        border.width: Theme.cardBorder
        border.color: root.accent ? Theme.primary : Theme.border
        antialiasing: true
    }

    component SmallText: Text {
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Math.round(Theme.fontSmallSize * Theme.fontSmallLineHeight)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    component CaptionText: Text {
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontCaptionSize
        renderType: Text.NativeRendering
    }

    component Heading: Text {
        textFormat: Text.PlainText
        color: Theme.fg
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontH3SubsectionSize
        font.weight: Font.Medium
        lineHeight: Math.round(Theme.fontH3SubsectionSize * Theme.fontH3SubsectionLineHeight)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        elide: Text.ElideRight
    }

    Column {
        id: body
        x: Theme.updatePanelPaddingX + Theme.cardBorder
        y: Theme.updatePanelPaddingY + Theme.cardBorder
        width: root.width - (Theme.updatePanelPaddingX + Theme.cardBorder) * 2

        // ── шапка: иконка 17 · заголовок h3 · бейдж · справа подпись 12 (_p2.py:235) ──
        RowLayout {
            width: parent.width
            spacing: 9 // _p2.py: зазор шапки панели.
            visible: root.panelState !== "unavailable"

            Icon {
                name: root.panelState === "uptodate" ? "check"
                    : root.panelState === "checking" ? "refresh" : ""
                size: 17
                color: root.panelState === "uptodate" ? Theme.successInk : Theme.fgMuted
                visible: name !== ""
                Layout.alignment: Qt.AlignVCenter
            }

            Heading {
                objectName: "updatePanelTitle"
                text: root.panelState === "uptodate" ? qsTr("Установлена последняя версия")
                    : root.panelState === "checking" ? qsTr("Проверяю обновления…")
                    : root.panelState === "skipped" ? qsTr("Версия %1 пропущена").arg(root.version)
                    : qsTr("Доступна версия %1").arg(root.version)
                Layout.maximumWidth: body.width
                Layout.alignment: Qt.AlignVCenter
            }

            // Бейдж «Новая» — primary-bg / primary (§4.8 «Рекомендуем», _p2.py `.bd.rec`).
            Rectangle {
                visible: root.panelState === "available"
                Layout.preferredWidth: badgeText.implicitWidth + Theme.badgePaddingX * 2
                Layout.preferredHeight: Math.ceil(Theme.badgeHeight)
                Layout.alignment: Qt.AlignVCenter
                radius: Theme.badgeRadius
                color: Theme.primaryBg
                antialiasing: true

                Text {
                    id: badgeText
                    anchors.centerIn: parent
                    textFormat: Text.PlainText
                    text: qsTr("Новая")
                    color: Theme.primary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.badgeSize
                    font.weight: Font.Medium
                    renderType: Text.NativeRendering
                }
            }

            Item { Layout.fillWidth: true }

            CaptionText {
                text: root.panelState === "uptodate" ? root.checkedText
                    : root.panelState === "skipped" ? qsTr("Напомним о следующей версии") : ""
                visible: text !== ""
                Layout.alignment: Qt.AlignVCenter
            }
        }

        // ── пояснение под шапкой (.sm, отступ 6) ────────────────────────────────
        Item { width: 1; height: 6; visible: explanation.visible }

        SmallText {
            id: explanation
            width: parent.width
            text: root.panelState === "checking" ? qsTr("Ответ ждём не дольше 3 секунд.")
                : root.panelState === "skipped" ? qsTr("Обновиться до неё всё ещё можно.")
                : ""
            visible: text !== ""
        }

        // uptodate: «Версия 0.2.0.» — номер моноширинным (сквозное правило 5), дальше пояснение.
        Item { width: 1; height: 6; visible: uptodateLine.visible }

        Flow {
            id: uptodateLine
            objectName: "updateInstalledLine"
            width: parent.width
            visible: root.panelState === "uptodate"

            SmallText {
                text: root.currentVersion !== "" ? qsTr("Версия") + " " : ""
                visible: root.currentVersion !== ""
                wrapMode: Text.NoWrap
            }

            SmallText {
                objectName: "updateInstalledVersion"
                text: root.currentVersion
                visible: root.currentVersion !== ""
                font.family: Theme.fontMono
                wrapMode: Text.NoWrap
            }

            SmallText {
                width: Math.min(implicitWidth, uptodateLine.width)
                text: (root.currentVersion !== "" ? ". " : "")
                    + (root.autoCheck
                        ? qsTr("Следующая автоматическая проверка — не раньше чем через сутки.")
                        : qsTr("Автоматическая проверка выключена — проверяйте вручную, когда удобно."))
            }
        }

        // ── «Что нового» (.sm fg-secondary полужирный, отступ 7; список 12.5 / 1.6, слева 17) ──
        Item { width: 1; height: 7; visible: whatsNewTitle.visible }

        SmallText {
            id: whatsNewTitle
            width: parent.width
            text: qsTr("Что нового")
            color: Theme.fgSecondary
            font.weight: Font.Bold
            visible: root.panelState === "available" && root.notes.length > 0
        }

        Item { width: 1; height: 6; visible: notesList.visible }

        // Список: маркер — отдельный элемент в отступе 17 (`.ul` padding-left, _shell.py:134),
        // текст с x = 17 и висячим отступом при переносе.
        Column {
            id: notesList
            objectName: "updateNotes"
            width: parent.width
            visible: whatsNewTitle.visible

            FontMetrics {
                id: capBox
                font.family: Theme.fontUi
                font.pixelSize: Theme.updatePanelListSize
            }

            Repeater {
                model: root.notes

                Item {
                    required property var modelData
                    readonly property int lineHeight:
                        Math.round(Theme.updatePanelListSize * Theme.updatePanelListLineHeight)

                    width: notesList.width
                    height: noteLine.implicitHeight

                    // Маркер — круг 4 × 4 в отступе (как маркер `ul` в макете): x = 2, центр на
                    // 5,5 px ниже верха заглавных букв первой строки пункта.
                    Rectangle {
                        objectName: "updateNoteMarker"
                        visible: parent.modelData.bullet === true
                        x: 2
                        y: Math.round(noteLine.baselineOffset + capBox.tightBoundingRect("Н").y
                                      + 5.5 - height / 2)
                        width: 4
                        height: 4
                        radius: 2
                        color: Theme.fgSecondary
                        antialiasing: true
                    }

                    Text {
                        id: noteLine
                        objectName: "updateNoteLine"
                        x: 17
                        width: parent.width - x
                        // Недоверенный текст из сети — строго простой текст (требование ревью 29.09).
                        textFormat: Text.PlainText
                        text: String(parent.modelData.text)
                        color: Theme.fgSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.updatePanelListSize
                        lineHeight: parent.lineHeight
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        // ── источник недоступен: баннер info с иконкой globe ─────────────────────
        NoteBanner {
            width: parent.width
            visible: root.panelState === "unavailable"
            height: visible ? implicitHeight : 0
            iconName: "globe"
            title: qsTr("Источник обновлений недоступен")
            body: qsTr("Не удалось связаться с сервером обновлений. Это не мешает работе — диктовка не зависит от сети.")

            AvButton {
                objectName: "updateRetry"
                iconName: "refresh"
                text: qsTr("Повторить")
                enabled: root.canCheckNow
                onClicked: root.checkRequested()
            }
        }

        // ── действия (отступ 12, зазор 8) ──────────────────────────────────────
        Item { width: 1; height: 12; visible: actions.visible }

        RowLayout {
            id: actions
            spacing: 8
            visible: root.panelState === "available" || root.panelState === "uptodate"
                || root.panelState === "skipped"

            AvButton {
                objectName: "updateReleasePage"
                visible: root.panelState === "available" && root.releasePageAvailable
                variant: "primary"
                iconName: "out"
                text: qsTr("Страница выпуска")
                onClicked: root.releasePageRequested()
            }

            AvButton {
                objectName: "updateSkip"
                visible: root.panelState === "available"
                text: qsTr("Пропустить эту версию")
                onClicked: root.skipRequested()
            }

            AvButton {
                objectName: "updateRemindLater"
                visible: root.panelState === "available"
                variant: "ghost"
                text: qsTr("Напомнить позже")
                onClicked: root.remindLaterRequested()
            }

            AvButton {
                objectName: "updateCheckAgain"
                visible: root.panelState === "uptodate"
                iconName: "refresh"
                text: qsTr("Проверить ещё раз")
                enabled: root.canCheckNow
                onClicked: root.checkRequested()
            }

            AvButton {
                objectName: "updateShowSkipped"
                visible: root.panelState === "skipped"
                text: qsTr("Показать %1").arg(root.version)
                onClicked: root.showSkippedRequested()
            }
        }
    }
}
