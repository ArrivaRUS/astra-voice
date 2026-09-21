// design/spec.md §5.1–5.5; design/mockups/final/08-onboarding-2-model.html (.mc).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Rectangle {
    id: root

    property string modelId: ""
    property string modelTitle: ""
    property string vendor: ""
    property string purpose: ""
    property string host: ""
    property bool recommended: false
    property string sizeText: ""
    property string ramText: ""
    property bool selected: false
    property string badge: ""
    property string cardState: "available"
    // Пока в мосте нет слота «открыть папку моделей», кнопка скрыта, чтобы не быть тихой заглушкой.
    property bool openFolderEnabled: false
    property string message: ""
    property var metrics: []
    property var tags: []

    signal toggleRequested()
    signal retryRequested()
    signal cancelRequested()
    signal openFolderRequested()

    readonly property bool selectionAvailable: badge === ""
        && (cardState === "available" || cardState === "failed")
    readonly property bool busy: cardState === "queued" || cardState === "downloading"
        || cardState === "verifying"
    readonly property bool hasError: cardState === "failed" || cardState === "no-space"
    readonly property string selectionMark: cardState === "no-space" ? "blocked"
        : badge !== "" || busy || cardState === "installed" ? "locked"
        : selected ? "on" : "off"
    readonly property string statusLabel: cardState === "downloading" ? qsTr("Загружается")
        : cardState === "queued" ? qsTr("В очереди")
        : cardState === "verifying" ? qsTr("Проверяю…")
        : badge === "active" ? qsTr("Установлена и активна")
        : badge === "installed" ? qsTr("Установлена") : ""
    readonly property bool hasMetricData: metrics.some(function(metric) {
        return metric.hasData === true;
    })
    readonly property real footerGap: 9 // spec §5.1: зазор нижнего блока.

    implicitWidth: Theme.onboardingStep2ContentW
    implicitHeight: footer.y + footer.height + Theme.modelCardPaddingY + Theme.cardBorder
    width: parent ? parent.width : implicitWidth
    height: implicitHeight
    radius: Theme.modelCardRadius
    color: selectionAvailable && cardMouse.pressed ? Theme.statePressedOnSurface
        : selectionAvailable && cardMouse.containsMouse ? Theme.stateHoverOnSurface
        : badge === "active" ? Theme.accentBg
        : selected || busy ? Theme.primaryBg : Theme.bgSurface
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

    component SpaceText: FooterText {
        color: Theme.modelCardSpaceLineLabel
        font.pixelSize: Theme.modelCardSpaceLineSize
        lineHeight: Theme.modelCardSpaceLineLineHeight
        wrapMode: Text.NoWrap
    }

    component Dot: Rectangle {
        width: 4 // spec §5.2: диаметр разделителя.
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
                Layout.preferredWidth: Theme.modelCardSelectSize
                Layout.preferredHeight: Theme.modelCardSelectSize
                Layout.alignment: Qt.AlignTop
                Layout.topMargin: 2
                radius: Theme.modelCardSelectRadius
                border.width: Theme.modelCardSelectBorder
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
                implicitHeight: purposeText.y + purposeText.height
                Flow {
                    id: nameRow
                    width: parent.width
                    spacing: Theme.modelCardHeadGap
                    FooterText {
                        text: root.modelTitle
                        width: Math.min(implicitWidth, nameRow.width)
                        color: Theme.fg
                        font.pixelSize: Theme.fontModelNameSize
                        font.weight: Font.Medium
                    }
                    FooterText {
                        visible: root.vendor !== ""
                        text: qsTr(" · %1").arg(root.vendor)
                        font.pixelSize: Theme.fontModelVendorSize
                        wrapMode: Text.NoWrap
                    }
                    CardBadge {
                        visible: root.recommended
                        text: qsTr("Рекомендуем")
                        color: Theme.primaryBg
                        textColor: Theme.primary
                    }
                    CardBadge {
                        visible: root.statusLabel !== ""
                        text: root.statusLabel
                        color: root.busy ? Theme.bgSurface2
                            : root.badge === "active" ? Theme.bgSurface : Theme.successBg
                        textColor: root.busy ? Theme.fgMuted
                            : root.badge === "active" ? Theme.accentInk : Theme.successInk
                    }
                }
                FooterText {
                    id: purposeText
                    y: nameRow.height + 2 // spec §5.2: отступ назначения.
                    width: parent.width
                    text: root.purpose
                    font.pixelSize: Theme.fontModelPurposeSize
                }
            }
        }

        Item {
            visible: root.metrics.length > 0
            Layout.alignment: Qt.AlignTop
            Layout.minimumWidth: implicitWidth
            implicitWidth: Math.max(metricRows.implicitWidth,
                sourceCaption.visible ? sourceCaption.implicitWidth : 0)
            implicitHeight: metricRows.height + (sourceCaption.visible
                ? Theme.modelCardMetricSourceCaptionMarginTop + sourceCaption.height : 0)
            Column {
                id: metricRows
                spacing: Theme.modelCardMetricsGap
                Repeater {
                    model: root.metrics
                    RowLayout {
                        spacing: Theme.modelCardMetricGap
                        FooterText {
                            Layout.preferredWidth: Theme.modelCardMetricLabelW
                            horizontalAlignment: Text.AlignRight
                            text: modelData.label
                            font.pixelSize: Theme.fontMetricSize
                        }
                        Rectangle {
                            Layout.preferredWidth: Theme.modelCardMetricTrackW
                            Layout.preferredHeight: Theme.modelCardMetricTrackH
                            radius: Theme.modelCardMetricTrackRadius
                            color: modelData.hasData === false ? "transparent" : Theme.modelCardMetricTrackBg
                            border.width: modelData.hasData === false ? 1 : 0
                            border.color: Theme.fgFaint
                            Rectangle {
                                visible: modelData.hasData !== false
                                width: parent.width * Math.max(0, Math.min(1, modelData.fill))
                                height: parent.height
                                radius: Theme.modelCardMetricTrackRadius
                                color: Theme.modelCardMetricFillEstimated
                            }
                        }
                        FooterText {
                            Layout.fillWidth: true
                            text: modelData.text
                            font.pixelSize: Theme.modelCardMetricValueSize
                            font.weight: Font.Normal
                            font.italic: modelData.hasData === false
                            color: modelData.hasData === false ? Theme.fgDisabled : Theme.fgMuted
                            wrapMode: Text.NoWrap
                        }
                    }
                }
            }
            FooterText {
                id: sourceCaption
                visible: root.hasMetricData
                y: metricRows.height + Theme.modelCardMetricSourceCaptionMarginTop
                width: parent.width
                horizontalAlignment: Text.AlignRight
                text: qsTr("цифры авторов, не с этого компьютера")
                font.pixelSize: Theme.modelCardMetricSourceCaptionSize
                color: Theme.modelCardMetricSourceCaptionColor
                wrapMode: Text.NoWrap
            }
        }
    }

    Rectangle {
        id: divider
        x: Theme.cardBorder + Theme.modelCardPaddingX
        y: top.y + top.height + 8 // spec §5.1: поле над разделителем.
        width: top.width
        height: 1 // spec §5.1: толщина разделителя.
        color: Theme.borderSoft
    }

    Column {
        id: footer
        x: Theme.cardBorder + Theme.modelCardPaddingX
        y: divider.y + divider.height + 7 // spec §5.1: поле под разделителем.
        width: top.width
        spacing: Theme.modelCardSpaceLineGapToTags

        Row {
            spacing: 0
            SpaceText { text: qsTr("Занимает места: ") }
            SpaceText {
                text: root.sizeText
                color: Theme.fg
            }
            SpaceText { text: qsTr(" на диске") }
            Item {
                width: 4 + 2 * root.footerGap
                height: Theme.modelCardSpaceLineLineHeight
                Dot { anchors.centerIn: parent }
            }
            SpaceText {
                text: root.ramText
                color: Theme.fg
            }
            SpaceText { text: qsTr(" в памяти при работе") }
        }

        RowLayout {
            id: bottomRow
            width: parent.width
            visible: root.tags.length > 0 || root.busy || root.hasError
            spacing: root.footerGap

            Flow {
                visible: root.tags.length > 0
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredHeight: height
                spacing: root.footerGap
                Repeater {
                    model: root.tags
                    RowLayout {
                        spacing: root.footerGap
                        Dot { visible: index !== 0 }
                        FooterText { text: modelData }
                    }
                }
            }

            Item {
                visible: root.tags.length === 0
                Layout.fillWidth: true
            }

            RowLayout {
                visible: root.busy || root.hasError
                Layout.maximumWidth: root.tags.length > 0 ? bottomRow.width * 0.65 : bottomRow.width
                spacing: root.footerGap
                Icon {
                    visible: root.hasError
                    name: "alert"
                    size: 12
                    color: Theme.dangerInk
                }
                FooterText {
                    visible: root.hasError
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: root.message !== "" ? root.message
                        : root.cardState === "no-space" ? qsTr("Не хватает места на диске")
                        : qsTr("Не удалось загрузить модель")
                    color: Theme.dangerInk
                }
                AvButton {
                    visible: root.cardState === "downloading" || root.cardState === "queued"
                    Layout.minimumWidth: implicitWidth
                    small: true
                    text: qsTr("Отмена")
                    onClicked: root.cancelRequested()
                }
                FooterText {
                    visible: root.cardState === "verifying"
                    text: qsTr("Отмена недоступна")
                    color: Theme.fgDisabled
                }
                AvButton {
                    visible: root.cardState === "failed"
                    Layout.minimumWidth: implicitWidth
                    small: true
                    variant: "primary"
                    text: qsTr("Повторить")
                    onClicked: root.retryRequested()
                }
                AvButton {
                    visible: root.cardState === "no-space" && root.openFolderEnabled
                    Layout.minimumWidth: implicitWidth
                    small: true
                    iconName: "folder"
                    text: qsTr("Открыть папку моделей")
                    onClicked: root.openFolderRequested()
                }
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
        border.color: root.badge === "active" ? Theme.accent
            : root.selected || root.busy ? Theme.primary : Theme.border
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
