// Строка-статус — design/spec.md §2. Высота 36, фон bg-app, граница сверху 1 px.
// Слева: иконка состояния + активная модель. Справа: состояние обновления · точка · версия (PT Mono).
// Справа — подмножество v0.2 спеки §2.2 (M7-ядро): disabled · policy-locked · checking ·
// uptodate (3000 мс, только после ручной проверки) · available · unavailable · error-net ·
// skipped; плюс idle — только версия. Состояния установки — с M8.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import "components"

Item {
    id: root

    // Левая часть (§2.1): loading | active | switching | error | none.
    property string modelState: "active"
    property string modelName: "GigaAM v3 RNN-T"
    property bool revocationUnknown: false
    // Правая часть (§2.2): значения — как у updatesBridge.state (docs/ui-bridge.md §3.8).
    property string updateState: "disabled"
    property string updateNetworkRefusal: ""
    property string updateDownloadPhase: "idle"
    property int updateDownloadPercent: 0
    property string updateVersion: ""
    // «Напомнить позже»: версия доступна, но без акцента.
    property bool updateSnoozed: false
    // Что показать после 3000 мс uptodate: idle (только версия) или disabled (тумблер выключен).
    property string updateRestState: "idle"
    property string version: "v0.2.0"

    // Клик или Enter/Space по кликабельному состоянию (§2.3: 5, 12, 15, 17).
    signal updateActivated(string kind)

    // «Установлена последняя версия» держится 3000 мс (token motion.duration.uptodate-message).
    property bool uptodateShown: false
    onUpdateStateChanged: {
        uptodateShown = updateState === "uptodate"
        if (uptodateShown)
            uptodateTimer.restart()
    }
    Component.onCompleted: {
        uptodateShown = updateState === "uptodate"
        if (uptodateShown)
            uptodateTimer.restart()
    }

    Timer {
        id: uptodateTimer
        interval: Theme.durationUptodateMessage
        repeat: false
        onTriggered: root.uptodateShown = false
    }

    readonly property string modelText: {
        switch (modelState) {
        case "loading": return qsTr("Загружаю %1…").arg(modelName);
        case "switching": return qsTr("Переключаю на %1…").arg(modelName);
        case "error": return qsTr("Модель не загрузилась — открыть Модели");
        case "none": return qsTr("Модель не выбрана — установить");
        default: return modelName;
        }
    }

    readonly property string warningText: revocationUnknown && modelState === "active"
        ? qsTr("Проверить отозванные версии сейчас нельзя") : ""

    readonly property string updateText: {
        switch (updateDownloadPhase) {
        case "metadata": return qsTr("Проверяю сведения об обновлении…");
        case "downloading": return qsTr("Скачиваю обновление · %1 %").arg(updateDownloadPercent);
        case "verifying": return qsTr("Проверяю обновление…");
        case "readydeb": return qsTr("Пакет проверен · Открыть обновления");
        case "readyappimage": return qsTr("Обновление готово · Открыть обновления");
        case "installing": return qsTr("Устанавливаю обновление…");
        case "error": return qsTr("Обновление не выполнено · Подробнее");
        }
        if (updateNetworkRefusal === "offline" && updateState === "available")
            return qsTr("Офлайн-режим · Подробнее");
        switch (updateState) {
        case "idle": return "";
        case "policy-locked": return qsTr("Проверка обновлений отключена (задано администратором)");
        case "checking": return qsTr("Проверяю обновления…");
        case "uptodate": return uptodateShown ? qsTr("Установлена последняя версия")
            : updateRestState === "disabled" ? qsTr("Проверка обновлений отключена") : "";
        case "available": return qsTr("Доступна версия %1 · Подробнее").arg(updateVersion);
        case "unavailable": return qsTr("Источник обновлений недоступен · Повторить");
        case "error-net": return qsTr("Не удалось скачать обновление · Повторить");
        case "skipped": return qsTr("Версия %1 пропущена · Показать").arg(updateVersion);
        default: return qsTr("Проверка обновлений отключена");
        }
    }
    readonly property string updateIcon: updateState === "policy-locked" ? "lock"
        : updateState === "uptodate" && uptodateShown ? "check"
        : updateState === "error-net" || updateDownloadPhase === "error" ? "alert" : ""
    // available — единственный цветовой акцент внизу окна (§2.2).
    readonly property bool updateAccent: updateState === "available" && updateDownloadPhase === "idle" && updateNetworkRefusal === "" && !updateSnoozed
    readonly property color updateColor: updateState === "error-net" || updateDownloadPhase === "error" ? Theme.dangerInk
        : updateAccent ? Theme.statusbarAccentFg : Theme.statusbarFg
    readonly property bool updateClickable: updateDownloadPhase !== "idle" || ["available", "unavailable", "error-net", "skipped"]
        .indexOf(updateState) >= 0
    // Строка перестала быть кликабельной — фокус с неё снимается, иначе он «висит» на тексте.
    onUpdateClickableChanged: {
        if (!updateClickable && updateItem.activeFocus)
            updateItem.focus = false
    }

    implicitHeight: Theme.statusbarH

    Rectangle {
        anchors.fill: parent
        color: Theme.statusbarBg

        Rectangle {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: Theme.borderHairline
            color: Theme.border
        }
    }

    Item {
        id: content
        anchors.fill: parent
        // Центрируем по 35 px под верхней границей 1 px, а не по всей высоте 36.
        anchors.topMargin: Theme.borderHairline
        anchors.leftMargin: Theme.cardRowPaddingX
        anchors.rightMargin: Theme.cardRowPaddingX

        RowLayout {
            id: modelRow
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: 7  // §2: зазор иконки трея до текста

            BrandMark {
                id: statusMark
                size: Theme.statusbarTrayIcon
                tray: true  // иконка состояния — мастер-геометрия 22 (§9.1), не логотип
                color: Theme.statusbarFg
                Layout.alignment: Qt.AlignVCenter
            }

            Text {
                objectName: "statusModelName"
                textFormat: Text.PlainText
                text: root.modelText
                elide: Text.ElideRight
                color: Theme.statusbarFg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontStatusbarSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: root.warningText
                    ? Math.max(0, content.width - updateRow.width - Theme.statusbarGap
                               - statusMark.width - modelRow.spacing * 3
                               - warningSeparator.implicitWidth - warningLabel.implicitWidth)
                    : Math.max(0, content.width - updateRow.width - Theme.statusbarGap - statusMark.width - modelRow.spacing)
            }

            Text {
                id: warningSeparator
                textFormat: Text.PlainText
                text: " · "
                visible: root.warningText !== ""
                color: Theme.statusbarFg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontStatusbarSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            Text {
                id: warningLabel
                objectName: "statusRevocationWarning"
                textFormat: Text.PlainText
                text: root.warningText
                visible: root.warningText !== ""
                color: Theme.statusbarFg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontStatusbarSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }
        }

        RowLayout {
            id: updateRow
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: 0

            // Состояние обновления: иконка 12 + текст, зазор 5 (_shell.py:298).
            Item {
                id: updateItem
                objectName: "statusUpdate"
                visible: root.updateText !== ""
                Layout.preferredWidth: Math.min(updateLabel.implicitWidth + (root.updateIcon !== "" ? 17 : 0), Math.max(0, content.width * 0.72 - 90))
                Layout.preferredHeight: updateLabel.implicitHeight
                Layout.alignment: Qt.AlignVCenter
                activeFocusOnTab: root.updateClickable
                Accessible.role: root.updateClickable ? Accessible.Button : Accessible.StaticText
                Accessible.name: root.updateText
                Accessible.onPressAction: {
                    if (root.updateClickable)
                        root.updateActivated(root.updateState)
                }

                Keys.onPressed: {
                    if (root.updateClickable && (event.key === Qt.Key_Return
                            || event.key === Qt.Key_Enter || event.key === Qt.Key_Space)) {
                        root.updateActivated(root.updateState)
                        event.accepted = true
                    }
                }

                RowLayout {
                    id: updateLine
                    anchors.fill: parent
                    spacing: 5

                    Icon {
                        name: root.updateIcon
                        size: 12
                        color: root.updateColor
                        visible: root.updateIcon !== ""
                        Layout.alignment: Qt.AlignVCenter
                    }

                    Text {
                        id: updateLabel
                        objectName: "statusUpdateText"
                        textFormat: Text.PlainText
                        text: root.updateText
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        color: root.updateColor
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontStatusbarSize
                        font.weight: root.updateAccent ? Font.Medium : Font.Normal
                        renderType: Text.NativeRendering
                        Layout.alignment: Qt.AlignVCenter
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: root.updateClickable
                    cursorShape: root.updateClickable ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: root.updateActivated(root.updateState)
                }

                // Кольцо фокуса по общему правилу (§2.3): 2 px, зазор 2, радиус 8.
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                    radius: Theme.focusRadius
                    color: "transparent"
                    border.width: Theme.focusWidth
                    border.color: Theme.stateFocusRing
                    antialiasing: true
                    visible: updateItem.activeFocus
                }
            }

            // Точка-разделитель 4 px с полями 8 (§2).
            Item {
                Layout.leftMargin: updateItem.visible ? Theme.statusbarGap : 0
                Layout.rightMargin: Theme.statusbarGap
                Layout.preferredWidth: Theme.statusbarSeparatorDot + Theme.titlebarGap * 2
                Layout.preferredHeight: Theme.statusbarSeparatorDot
                Layout.alignment: Qt.AlignVCenter

                Rectangle {
                    anchors.centerIn: parent
                    width: Theme.statusbarSeparatorDot
                    height: width
                    radius: width / 2
                    color: Theme.fgFaint
                }
            }

            Text {
                textFormat: Text.PlainText
                text: root.version
                color: Theme.statusbarFg
                font.family: Theme.fontMono
                font.pixelSize: Theme.fontStatusbarNumSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }
}
