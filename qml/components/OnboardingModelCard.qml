// design/spec.md §5.1–5.5; design/mockups/final/08-onboarding-2-model.html (.mc).
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."

Rectangle {
    id: root

    property string modelId: ""
    property string modelTitle: ""
    property string vendor: ""
    property string vendorShort: ""
    property string purpose: ""
    property string host: ""
    property bool recommended: false
    property string sizeText: ""
    property string ramText: ""
    property bool measurementMode: false
    property int ramMb: 0
    property bool ramMeasured: false
    readonly property bool showRam: measurementMode
        ? (ramMb !== 0 || ramMeasured) : ramText !== ""
    property bool selected: false
    property string badge: ""
    property string cardState: "available"
    property bool canCancel: cardState === "downloading" || cardState === "paused-no-space"
    property bool canRetry: cardState === "failed" || cardState === "paused-no-space"
    property bool canDequeue: cardState === "queued"
    // Кнопку показываем только там, где мост умеет открыть папку.
    property bool openFolderEnabled: false
    // Управление установленной моделью есть в разделе «Модели» и нет в мастере.
    property bool manageEnabled: false
    property bool updateAvailable: false
    property string message: ""
    property string hint: ""
    property string hintKind: ""
    property bool memoryShortage: false
    property bool canSwitchWithPause: false
    property bool canReinstall: true
    // Вернуть, когда мост выдаёт эти состояния и у действий появятся обработчики.
    property bool detailsActionAvailable: false
    property bool updateActionsAvailable: false
    property var metrics: []
    property var tags: []

    signal toggleRequested()
    signal retryRequested()
    signal cancelRequested()
    signal dequeueRequested()
    signal openFolderRequested()
    signal activateRequested()
    signal removeRequested()
    signal updateRequested()
    signal switchWithPauseRequested()
    signal reinstallRequested()
    signal detailsRequested()

    readonly property bool selectionAvailable: badge === ""
        && ["available", "failed", "sha-failed", "new", "low-ram",
            "not-recommended", "no-benchmark"].indexOf(cardState) >= 0
    readonly property bool highlighted: badge === "" && (selected || cardState === "paused-no-space")
    readonly property bool manageVisible: manageEnabled && badge !== ""
    readonly property bool busy: cardState === "queued" || cardState === "downloading"
        || cardState === "verifying"
    readonly property bool hasError: ["failed", "sha-failed", "no-space", "paused-no-space",
        "broken", "corrupted", "update-failed"].indexOf(cardState) >= 0
    // Отказ переключения или удаления приходит сообщением на исправной карточке.
    readonly property bool hasMessage: hasError || message !== ""
        || ["no-network", "offline-user", "policy"].indexOf(cardState) >= 0
    readonly property bool neutralMessage: ["no-network", "offline-user", "policy"]
        .indexOf(cardState) >= 0
    readonly property bool messageError: hasMessage && !neutralMessage
    readonly property bool unavailable: ["no-space", "no-network", "offline-user", "policy"]
        .indexOf(cardState) >= 0
    readonly property bool showHint: (hint !== "" || ["low-ram", "not-recommended",
        "no-benchmark"].indexOf(cardState) >= 0) && (!hasMessage || memoryShortage)
    readonly property bool footerHasButtons: cardState === "queued"
        || cardState === "downloading" || cardState === "paused-no-space"
        || cardState === "failed"
        || cardState === "sha-failed"
        || (updateActionsAvailable && (cardState === "updating"
            || cardState === "update-failed"))
        || (cardState === "no-space" && openFolderEnabled) || manageVisible
    readonly property string selectionMark: ["no-space", "no-network", "offline-user",
        "policy"].indexOf(cardState) >= 0 ? "blocked"
        : badge !== "" || busy || cardState === "installed"
            || cardState === "paused-no-space" ? "locked"
        : selected ? "on" : "off"
    readonly property real selectBorderWidth: Theme.modelCardSelectBorder
    readonly property real footerIndent: Theme.modelCardSelectSize + Theme.modelCardSelectGap
    readonly property string statusLabel: cardState === "downloading" ? qsTr("Загружается")
        : cardState === "queued" ? qsTr("В очереди")
        : cardState === "verifying" ? qsTr("Проверяю…")
        : cardState === "switching" ? qsTr("Переключаю…")
        : cardState === "new" ? qsTr("Новое")
        : cardState === "updating" ? qsTr("Обновляю…")
        : cardState === "update-available" ? qsTr("Обновление доступно")
        : badge === "active" ? qsTr("Установлена и активна")
        : badge === "installed" ? qsTr("Установлена") : ""

    implicitWidth: Theme.onboardingStep2ContentW
    implicitHeight: footer.y + footer.height + Theme.modelCardPaddingY + Theme.cardBorder
    width: parent ? parent.width : implicitWidth
    height: implicitHeight
    radius: Theme.modelCardRadius
    color: selectionAvailable && cardMouse.pressed ? Theme.statePressedOnSurface
        : selectionAvailable && cardMouse.containsMouse ? Theme.stateHoverOnSurface
        : badge === "active" ? Theme.accentBg
        : highlighted || busy ? Theme.primaryBg : Theme.bgSurface
    activeFocusOnTab: selectionAvailable
    onSelectionAvailableChanged: {
        if (!selectionAvailable)
            focus = false;
    }
    Keys.onPressed: {
        if (root.activeFocus && root.selectionAvailable
                && (event.key === Qt.Key_Space || event.key === Qt.Key_Return
                    || event.key === Qt.Key_Enter)) {
            if (!event.isAutoRepeat)
                root.toggleRequested();
            event.accepted = true;
        }
    }

    // Под содержимым: кнопки действий принимают нажатие раньше карточки.
    MouseArea {
        id: cardMouse
        anchors.fill: parent
        enabled: root.selectionAvailable
        hoverEnabled: root.selectionAvailable
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.forceActiveFocus(Qt.MouseFocusReason);
            root.toggleRequested();
        }
    }

    // Inline components доступны в Qt 5.15 и не требуют записи в qmldir.
    component FooterText: Text {
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontModelFooterSize
        font.weight: Font.Normal
        lineHeight: font.pixelSize * Theme.fontBodyLineHeight
        lineHeightMode: Text.FixedHeight
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
        renderType: Text.NativeRendering
    }

    component Dot: Rectangle {
        width: Theme.modelCardFooterDotSize
        height: width
        radius: width / 2
        color: Theme.fgFaint
    }

    component CardBadge: Rectangle {
        property alias text: badgeText.text
        property alias textColor: badgeText.color
        width: badgeText.implicitWidth + 2 * Theme.badgePaddingX
        height: Math.ceil(Theme.badgeHeight)
        radius: Theme.badgeRadius
        Text {
            id: badgeText
            x: Theme.badgePaddingX
            y: Theme.badgePaddingY
            height: parent.height - 2 * Theme.badgePaddingY
            verticalAlignment: Text.AlignVCenter
            font.family: Theme.fontUi
            font.pixelSize: Theme.badgeSize
            font.weight: Font.Medium
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
        }
    }

    RowLayout {
        id: top
        objectName: "cardTop"
        x: Theme.cardBorder + Theme.modelCardPaddingX
        y: Theme.cardBorder + Theme.modelCardPaddingY
        width: root.width - 2 * (Theme.cardBorder + Theme.modelCardPaddingX)
        spacing: Theme.modelCardTopGap

        RowLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignTop
            spacing: Theme.modelCardSelectGap

            Rectangle {
                objectName: "selectionBox"
                Layout.preferredWidth: Theme.modelCardSelectSize
                Layout.preferredHeight: Theme.modelCardSelectSize
                Layout.alignment: Qt.AlignTop
                Layout.topMargin: 2
                radius: Theme.modelCardSelectRadius
                border.width: root.selectBorderWidth
                antialiasing: false
                border.color: root.selectionMark === "on" ? Theme.primary
                    : root.selectionMark === "off" ? Theme.fgFaint : Theme.border
                color: root.selectionMark === "on" ? Theme.primary
                    : root.selectionMark === "off" ? Theme.bgSurface : Theme.bgSurface2
                Icon {
                    anchors.centerIn: parent
                    name: "check"
                    size: 12
                    color: root.selectionMark === "on" ? Theme.primaryFg : Theme.fgDisabled
                    visible: root.selectionMark === "on" || root.selectionMark === "locked"
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignTop
                implicitHeight: Math.max(Theme.modelCardHeadMinH, nameRow.height)
                    + purposeText.height
                Flow {
                    id: nameRow
                    width: parent.width
                    spacing: Theme.modelCardHeadGap
                    FooterText {
                        objectName: "modelName"
                        text: root.modelTitle
                        width: Math.min(implicitWidth, nameRow.width)
                        color: root.unavailable ? Theme.fgDisabled : Theme.fg
                        font.pixelSize: Theme.fontModelNameSize
                        font.weight: Font.Medium
                        lineHeight: Theme.modelCardHeadMinH
                    }
                    FooterText {
                        visible: root.vendorShort !== ""
                        text: qsTr("· %1").arg(root.vendorShort)
                        color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted
                        font.pixelSize: Theme.fontModelVendorSize
                        lineHeight: Theme.modelCardHeadMinH
                        wrapMode: Text.NoWrap
                        ToolTip {
                            visible: vendorHover.containsMouse && root.vendor !== ""
                            text: root.vendor
                            contentItem: Text {
                                text: root.vendor
                                textFormat: Text.PlainText
                                color: Theme.fg
                                font.family: Theme.fontUi
                            }
                        }
                        MouseArea {
                            id: vendorHover
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.NoButton
                        }
                    }
                    CardBadge {
                        visible: root.recommended
                        text: qsTr("Рекомендуем")
                        color: root.highlighted ? Theme.bgSurface : Theme.primaryBg
                        textColor: Theme.primary
                    }
                    CardBadge {
                        visible: root.statusLabel !== ""
                        text: root.statusLabel
                        color: root.busy || root.cardState === "new" ? Theme.bgSurface2
                            : root.badge === "active" ? Theme.bgSurface : Theme.successBg
                        textColor: root.busy || root.cardState === "new" ? Theme.fgMuted
                            : root.badge === "active" ? Theme.accentInk : Theme.successInk
                    }
                }
                FooterText {
                    id: purposeText
                    y: Math.max(Theme.modelCardHeadMinH, nameRow.height)
                    width: parent.width
                    text: root.purpose
                    color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted
                    font.pixelSize: Theme.fontModelPurposeSize
                    lineHeight: Theme.modelCardPurposeLineHeight
                }
            }
        }

        Item {
            visible: root.metrics.length > 0 && root.cardState !== "custom"
            Layout.alignment: Qt.AlignTop
            Layout.minimumWidth: implicitWidth
            implicitWidth: Theme.modelCardMetricLabelW + Theme.modelCardMetricLabelMarginRight
                + 2 * Theme.modelCardMetricGap + Theme.modelCardMetricTrackW
                + Theme.modelCardMetricValueW
            implicitHeight: metricRows.height
            Column {
                id: metricRows
                spacing: Theme.modelCardMetricsGap
                Repeater {
                    model: root.metrics
                    Item {
                        property bool metricHasData: modelData.hasData === true
                        width: Theme.modelCardMetricLabelW + Theme.modelCardMetricLabelMarginRight
                            + 2 * Theme.modelCardMetricGap + Theme.modelCardMetricTrackW
                            + Theme.modelCardMetricValueW
                        height: Theme.modelCardMetricRowH
                        RowLayout {
                            spacing: Theme.modelCardMetricGap
                            width: parent.width
                            height: parent.height
                            FooterText {
                                Layout.preferredWidth: Theme.modelCardMetricLabelW
                                Layout.rightMargin: Theme.modelCardMetricLabelMarginRight
                                horizontalAlignment: Text.AlignRight
                                text: modelData.label
                                color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted
                                font.pixelSize: Theme.fontMetricSize
                                wrapMode: Text.NoWrap
                            }
                            Rectangle {
                                Layout.preferredWidth: Theme.modelCardMetricTrackW
                                Layout.preferredHeight: Theme.modelCardMetricTrackH
                                radius: Theme.modelCardMetricTrackRadius
                                color: metricHasData ? Theme.modelCardMetricTrackBg : "transparent"
                                border.width: metricHasData ? 0 : 1
                                border.color: Theme.modelCardMetricNoDataBorder
                                Rectangle {
                                    objectName: "metricFill"
                                    visible: metricHasData
                                    width: parent.width * Math.max(0, Math.min(1,
                                        modelData.fill))
                                    height: parent.height
                                    radius: Theme.modelCardMetricTrackRadius
                                    color: modelData.level === "good" ? Theme.modelCardMetricFillGood
                                        : modelData.level === "fair" ? Theme.modelCardMetricFillFair
                                        : modelData.level === "weak" ? Theme.modelCardMetricFillWeak
                                        : Theme.fgFaint
                                }
                            }
                            Row {
                                Layout.preferredWidth: Theme.modelCardMetricValueW
                                spacing: Theme.modelCardMetricMeasuredMarkGap
                                FooterText {
                                    text: metricHasData ? modelData.text : qsTr("нет данных")
                                    font.pixelSize: Theme.modelCardMetricValueSize
                                    font.weight: modelData.measured === true ? Font.Medium : Font.Normal
                                    font.italic: !metricHasData
                                    color: root.unavailable ? Theme.fgDisabled
                                        : !metricHasData ? Theme.modelCardMetricNoDataValueColor
                                        : modelData.measured === true
                                            ? Theme.modelCardMetricMeasuredValueColor
                                            : Theme.modelCardMetricValueColor
                                    wrapMode: Text.NoWrap
                                    ToolTip.visible: modelData.measured === true
                                        && metricValueHover.containsMouse
                                    ToolTip.text: qsTr("замерено на этом компьютере")
                                    MouseArea {
                                        id: metricValueHover
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        acceptedButtons: Qt.NoButton
                                    }
                                }
                                Text {
                                    visible: modelData.measured === true && metricHasData
                                    text: "✓"
                                    color: Theme.modelCardMetricMeasuredMark
                                    font.pixelSize: Theme.modelCardMetricMeasuredMarkSize
                                    font.weight: Font.Bold
                                    ToolTip.visible: metricMarkHover.containsMouse
                                    ToolTip.text: qsTr("замерено на этом компьютере")
                                    MouseArea {
                                        id: metricMarkHover
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        acceptedButtons: Qt.NoButton
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        id: footer
        objectName: "cardFooter"
        x: Theme.cardBorder + Theme.modelCardPaddingX + root.footerIndent
        y: top.y + top.height + Theme.modelCardFooterGap
        width: top.width - root.footerIndent
        spacing: Theme.modelCardFooterColsGap
        Flow {
            id: footerFacts
            objectName: "footerFacts"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredHeight: implicitHeight
            Layout.alignment: Qt.AlignTop
            Layout.topMargin: root.footerHasButtons ? Theme.modelCardFooterBtnTextOffset : 0
            spacing: 0

            Row {
                spacing: 0
                FooterText { text: root.sizeText; color: root.unavailable ? Theme.fgDisabled : Theme.fg; wrapMode: Text.NoWrap }
                FooterText { text: qsTr(" на диске"); color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted; wrapMode: Text.NoWrap }
                Item { width: Theme.modelCardFooterDotGap; height: 1 }
            }
            Item {
                visible: root.showRam
                width: Theme.modelCardFooterDotSize + Theme.modelCardFooterDotGap
                height: Theme.modelCardSpaceLineLineHeight
                Dot { x: 0; anchors.verticalCenter: parent.verticalCenter }
            }
            Row {
                visible: root.showRam
                spacing: 0
                FooterText {
                    text: root.ramText
                    color: root.unavailable ? Theme.fgDisabled : Theme.fg
                    font.weight: root.ramMeasured ? Font.Medium : Font.Normal
                    wrapMode: Text.NoWrap
                    ToolTip.visible: root.ramMeasured && ramValueHover.containsMouse
                    ToolTip.text: qsTr("замерено на этом компьютере")
                    MouseArea {
                        id: ramValueHover
                        anchors.fill: parent
                        hoverEnabled: true
                        acceptedButtons: Qt.NoButton
                    }
                }
                FooterText { text: qsTr(" в памяти"); color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted; wrapMode: Text.NoWrap }
                Text {
                    visible: root.ramMeasured
                    text: "✓"
                    color: Theme.modelCardMetricMeasuredMark
                    font.pixelSize: Theme.modelCardMetricMeasuredMarkSize
                    font.weight: Font.Bold
                    ToolTip.visible: ramMarkHover.containsMouse
                    ToolTip.text: qsTr("замерено на этом компьютере")
                    MouseArea {
                        id: ramMarkHover
                        anchors.fill: parent
                        hoverEnabled: true
                        acceptedButtons: Qt.NoButton
                    }
                }
                Item { width: Theme.modelCardFooterDotGap; height: 1 }
            }
            Repeater {
                model: root.tags
                Row {
                    spacing: 0
                    Item {
                        width: Theme.modelCardFooterDotSize + Theme.modelCardFooterDotGap
                        height: Theme.modelCardSpaceLineLineHeight
                        Dot { x: 0; anchors.verticalCenter: parent.verticalCenter }
                    }
                    FooterText { text: modelData; color: root.unavailable ? Theme.fgDisabled : Theme.fgMuted; wrapMode: Text.NoWrap }
                    Item { width: Theme.modelCardFooterDotGap; height: 1 }
                }
            }
        }

        RowLayout {
            objectName: "footerActions"
            visible: root.busy || root.hasMessage || root.showHint || root.manageVisible
                || (root.cardState === "updating" && root.updateActionsAvailable)
            Layout.maximumWidth: footer.width * Theme.modelCardFooterActionsMaxShare
            Layout.preferredWidth: {
                var total = 0;
                var count = 0;
                for (var i = 0; i < children.length; ++i) {
                    if (children[i].visible && children[i].implicitWidth > 0) {
                        total += children[i].implicitWidth;
                        ++count;
                    }
                }
                return total + Math.max(0, count - 1) * spacing + Theme.cardBorder;
            }
            Layout.alignment: Qt.AlignTop
            spacing: Theme.modelCardFooterActionsGap
            Icon {
                objectName: "messageAlert"
                visible: root.messageError
                name: "alert"
                size: Theme.modelCardFooterMsgIconSize
                color: Theme.dangerInk
            }
            FooterText {
                objectName: "messageText"
                visible: root.hasMessage
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: root.message !== "" ? root.message
                    : root.cardState === "no-network"
                        ? qsTr("Нет доступа к источнику модели")
                    : root.cardState === "offline-user"
                        ? qsTr("Включена работа без сети")
                    : root.cardState === "policy"
                        ? qsTr("Выбор ограничен администратором")
                    : root.cardState === "no-space" || root.cardState === "paused-no-space"
                        ? qsTr("Не хватает места на диске")
                    : root.cardState === "sha-failed"
                        ? qsTr("Файл не прошёл проверку — загруженное удалено")
                    : root.cardState === "broken" || root.cardState === "corrupted"
                        ? qsTr("Файлы модели не читаются")
                    : root.cardState === "update-failed"
                        ? qsTr("Не удалось обновить модель")
                    : qsTr("Не удалось загрузить модель")
                color: root.messageError ? Theme.dangerInk : Theme.fgMuted
            }
            Icon {
                visible: root.showHint && (root.hintKind === "warning"
                    || root.cardState === "low-ram" || root.cardState === "not-recommended")
                name: "alert"
                size: Theme.modelCardFooterMsgIconSize
                color: Theme.warningInk
            }
            Icon {
                visible: root.showHint && (root.hintKind === "info"
                    || root.cardState === "no-benchmark")
                name: "info"
                size: Theme.modelCardFooterMsgIconSize
                color: Theme.fgMuted
            }
            FooterText {
                visible: root.showHint
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: root.hint !== "" ? root.hint
                    : root.cardState === "low-ram" ? qsTr("Может не хватить памяти")
                    : root.cardState === "not-recommended"
                        ? qsTr("Хуже подходит для русского языка")
                    : qsTr("Для этой модели пока нет цифр")
                color: root.hintKind === "warning" || root.cardState === "low-ram"
                    || root.cardState === "not-recommended"
                    ? Theme.warningInk : Theme.fgMuted
            }
            FooterText {
                visible: root.cardState === "verifying"
                text: qsTr("Отмена недоступна")
                color: Theme.fgDisabled
            }
            AvButton {
                visible: ((root.cardState === "failed" || root.cardState === "sha-failed"
                    || root.cardState === "paused-no-space") && root.canRetry)
                    || (root.cardState === "update-failed" && root.updateActionsAvailable)
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                variant: "primary"
                text: qsTr("Повторить")
                onClicked: root.retryRequested()
            }
            AvButton {
                visible: (root.cardState === "downloading" && root.canCancel)
                    || (root.cardState === "queued" && root.canDequeue)
                    || (root.cardState === "paused-no-space" && root.canCancel)
                    || (root.cardState === "updating" && root.updateActionsAvailable)
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                text: qsTr("Отмена")
                onClicked: {
                    if (root.cardState === "queued")
                        root.dequeueRequested();
                    else
                        root.cancelRequested();
                }
            }
            AvButton {
                visible: root.cardState === "sha-failed" && root.detailsActionAvailable
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                text: qsTr("Подробнее")
                onClicked: root.detailsRequested()
            }
            AvButton {
                visible: root.cardState === "no-space" && root.openFolderEnabled
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                iconName: "folder"
                text: qsTr("Открыть папку моделей")
                onClicked: root.openFolderRequested()
            }

            // Кнопки установленной модели (§5.4, состояния 19–20, 25).
            AvButton {
                visible: root.manageVisible && root.updateAvailable
                    && root.cardState !== "switching" && root.cardState !== "broken"
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                iconName: "down"
                text: qsTr("Обновить")
                onClicked: root.updateRequested()
            }
            AvButton {
                visible: root.manageVisible && root.badge !== "active"
                    && root.cardState !== "switching" && root.cardState !== "broken"
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                variant: "primary"
                text: qsTr("Сделать рабочей")
                onClicked: root.activateRequested()
            }
            AvButton {
                visible: root.manageVisible && root.canSwitchWithPause
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                text: qsTr("Переключить с паузой")
                onClicked: root.switchWithPauseRequested()
            }
            AvButton {
                visible: root.manageVisible
                    && (root.cardState === "broken" || root.cardState === "corrupted")
                    && root.canReinstall
                Layout.minimumWidth: visible ? implicitWidth : 0
                small: true
                variant: "primary"
                text: qsTr("Переустановить")
                onClicked: root.reinstallRequested()
            }
            AvButton {
                visible: root.manageVisible
                enabled: root.cardState !== "switching" && root.badge !== "active"
                Layout.minimumWidth: visible ? implicitWidth : 0
                Layout.alignment: Qt.AlignRight | Qt.AlignTop
                Layout.rightMargin: Theme.cardBorder
                small: true
                text: qsTr("Удалить")
                onClicked: root.removeRequested()
            }
        }
    }

    // Как в SettingGroup: полупрозрачная обводка смешивается с заливкой карточки,
    // а не с фоном окна. Rectangle поверх содержимого не перехватывает мышь.
    Rectangle {
        anchors.fill: parent
        radius: parent.radius
        color: "transparent"
        border.width: Theme.cardBorder
        border.color: root.badge === "active" || root.cardState === "switching" ? Theme.accent
            : root.highlighted || root.busy ? Theme.primary : Theme.border
        antialiasing: true
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        antialiasing: true
        visible: root.activeFocus
    }
}
