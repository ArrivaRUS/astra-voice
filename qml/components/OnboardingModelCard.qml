// design/spec.md §5.1–5.5; design/mockups/final/08-onboarding-2-model.html (.mc).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Rectangle {
    id: root

    property string modelState: "downloadable"
    property string modelName: qsTr("GigaAM v3 RNN-T")
    property string vendor: qsTr("Сбер (GigaChat Team)")
    property string purpose: qsTr("Русская диктовка с пунктуацией — по умолчанию")
    property string modelSize: qsTr("231,9 МБ")
    property string modelHost: qsTr("huggingface.co")
    property real progress: 0
    property string speed: ""
    property string eta: ""

    signal downloadRequested()
    signal cancelRequested()
    signal installFromPathRequested()
    signal retryRequested()
    signal openFolderRequested()
    signal removeDownloadRequested()

    readonly property bool installed: modelState === "installed"
    readonly property bool transferring: modelState === "downloading"
    readonly property bool busy: transferring || modelState === "verifying" || modelState === "installing"
    readonly property bool failed: modelState === "broken" || modelState === "no-space"
    readonly property bool warning: modelState === "no-ram"
    readonly property bool offline: modelState === "no-network"
    readonly property bool available: !installed && !busy && !failed && !warning && !offline
    readonly property real boundedProgress: Math.max(0, Math.min(1, progress))
    readonly property real footerGap: 9 // spec §5.1: зазор нижнего блока.

    implicitWidth: 620 // Макет 08-onboarding-2-model.html: ширина карточки шага 2.
    implicitHeight: footer.y + footer.height + Theme.modelCardPaddingY
    width: parent ? parent.width : implicitWidth
    height: implicitHeight
    radius: Theme.modelCardRadius
    border.width: 1 // spec §5.1: граница карточки.
    border.color: installed ? Theme.accent : Theme.primary
    color: installed ? Theme.accentBg : Theme.bgSurface

    // Inline components доступны в Qt 5.15 и не требуют записи в qmldir.
    component FooterText: Text {
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontModelFooterSize
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
        renderType: Text.NativeRendering
    }

    component Dot: Rectangle {
        width: 4 // spec §5.2 / макет .dot: диаметр разделителя.
        height: width
        radius: width / 2
        color: Theme.fgFaint
    }

    component SmallButton: AvButton {
        small: true
        Layout.minimumWidth: implicitWidth
    }

    RowLayout {
        id: top
        x: Theme.modelCardPaddingX
        y: Theme.modelCardPaddingY
        width: root.width - 2 * Theme.modelCardPaddingX
        spacing: Theme.modelCardTopGap

        Item {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignTop
            implicitHeight: badges.y + badges.height
            Flow {
                id: nameRow
                width: parent.width
                spacing: 0
                Text {
                    text: root.modelName
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontModelNameSize
                    font.weight: Font.Medium
                    textFormat: Text.PlainText
                    renderType: Text.NativeRendering
                }
                Text {
                    text: qsTr(" · %1").arg(root.vendor)
                    color: Theme.fgMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontModelVendorSize
                    wrapMode: Text.NoWrap
                    textFormat: Text.PlainText
                    renderType: Text.NativeRendering
                }
            }
            Text {
                id: purposeText
                y: nameRow.height + 2 // spec §5.2: отступ назначения.
                width: parent.width
                text: root.purpose
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontModelPurposeSize
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                renderType: Text.NativeRendering
            }
            Row {
                id: badges
                y: purposeText.y + purposeText.height + 4 // spec §5.2: отступ бейджей.
                spacing: Theme.modelCardBadgesGap
                Repeater {
                    model: root.installed ? [qsTr("Активна"), qsTr("Рекомендуем")] : [qsTr("Рекомендуем")]
                    Rectangle {
                        readonly property bool activeBadge: root.installed && index === 0
                        width: badgeText.implicitWidth + 2 * Theme.badgePaddingX
                        height: Theme.badgeHeight
                        radius: Theme.badgeRadius
                        color: activeBadge ? Theme.accentBg : Theme.primaryBg
                        Text {
                            id: badgeText
                            x: Theme.badgePaddingX
                            y: Theme.badgePaddingY
                            height: parent.height - 2 * Theme.badgePaddingY
                            verticalAlignment: Text.AlignVCenter
                            text: modelData
                            color: parent.activeBadge ? Theme.accentInk : Theme.primary
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.badgeSize
                            font.weight: Font.Medium
                            textFormat: Text.PlainText
                            renderType: Text.NativeRendering
                        }
                    }
                }
            }
        }

        Column {
            Layout.alignment: Qt.AlignTop
            Layout.minimumWidth: implicitWidth
            spacing: Theme.modelCardMetricsGap
            Repeater {
                model: [
                    { label: qsTr("Качество"), fill: 0.90, prefix: qsTr("WER "), number: qsTr("7,60 %"), suffix: "" },
                    { label: qsTr("Скорость"), fill: 0.50, prefix: "", number: qsTr("42,5×"), suffix: qsTr(" быстрее речи") }
                ]
                RowLayout {
                    spacing: Theme.modelCardMetricGap
                    Text {
                        Layout.preferredWidth: Theme.modelCardMetricLabelW
                        horizontalAlignment: Text.AlignRight
                        text: modelData.label
                        color: Theme.fgMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontMetricSize
                        textFormat: Text.PlainText
                        renderType: Text.NativeRendering
                    }
                    Rectangle {
                        width: Theme.modelCardMetricTrackW
                        height: Theme.modelCardMetricTrackH
                        radius: Theme.modelCardMetricTrackRadius
                        color: Theme.modelCardMetricTrackBg
                        Rectangle {
                            width: parent.width * modelData.fill
                            height: parent.height
                            radius: Theme.modelCardMetricTrackRadius
                            color: Theme.modelCardMetricFillMeasured
                        }
                    }
                    Row {
                        FooterText {
                            text: modelData.prefix
                            font.pixelSize: Theme.modelCardMetricValueSize
                        }
                        FooterText {
                            text: modelData.number
                            color: Theme.fg
                            font.family: Theme.fontMono
                            font.weight: Font.Bold
                            font.pixelSize: Theme.modelCardMetricValueSize
                        }
                        FooterText {
                            text: modelData.suffix
                            font.pixelSize: Theme.modelCardMetricValueSize
                        }
                    }
                }
            }
        }
    }

    Rectangle {
        id: divider
        x: Theme.modelCardPaddingX
        y: top.y + top.height + 8 // spec §5.1: поле над разделителем.
        width: top.width
        height: 1 // spec §5.1: толщина разделителя.
        color: Theme.borderSoft
    }

    Column {
        id: footer
        x: Theme.modelCardPaddingX
        y: divider.y + divider.height + 7 // spec §5.1: поле под разделителем.
        width: top.width
        spacing: root.footerGap

        Column {
            width: parent.width
            visible: root.busy
            spacing: 7 // spec §5.5: отступ под прогрессом.
            Rectangle {
                width: parent.width
                height: Theme.progressH
                radius: Theme.progressRadius
                color: Theme.progressBg
                Rectangle {
                    width: parent.width * (root.transferring ? root.boundedProgress : 1)
                    height: parent.height
                    radius: Theme.progressRadius
                    color: Theme.progressFill
                }
            }
            RowLayout {
                width: parent.width
                spacing: root.footerGap
                RowLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: 0
                    FooterText {
                        text: root.transferring ? qsTr("Загрузка ")
                            : root.modelState === "verifying" ? qsTr("Проверяю контрольную сумму…") : qsTr("Устанавливаю…")
                        Layout.fillWidth: !root.transferring
                        elide: Text.ElideRight
                        wrapMode: Text.NoWrap
                    }
                    FooterText {
                        visible: root.transferring
                        text: qsTr("%1 %").arg(Math.round(root.boundedProgress * 100))
                        font.family: Theme.fontMono
                    }
                    FooterText {
                        visible: root.transferring && root.speed !== ""
                        text: qsTr(" · ")
                    }
                    FooterText {
                        visible: root.transferring && root.speed !== ""
                        text: root.speed
                        font.family: Theme.fontMono
                    }
                    FooterText {
                        visible: root.transferring && root.eta !== ""
                        text: qsTr(" · осталось ")
                    }
                    FooterText {
                        visible: root.transferring && root.eta !== ""
                        text: root.eta
                        font.family: Theme.fontMono
                    }
                }
                Dot { visible: root.transferring }
                FooterText {
                    visible: root.transferring
                    text: root.modelHost
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    elide: Text.ElideRight
                    wrapMode: Text.NoWrap
                }
                Item { Layout.fillWidth: true }
                AvButton {
                    small: true
                    Layout.minimumWidth: implicitWidth
                    visible: root.transferring
                    text: qsTr("Отмена")
                    onClicked: root.cancelRequested()
                }
                FooterText {
                    visible: !root.transferring
                    text: qsTr("Отмена недоступна")
                    color: Theme.fgDisabled
                }
            }
        }

        FooterText {
            width: parent.width
            visible: root.modelState === "cancelled"
            text: qsTr("Загрузка отменена")
        }

        Flow {
            id: tagsRow
            width: parent.width
            visible: root.available || root.installed
            spacing: root.footerGap
            Row {
                id: tags
                spacing: root.footerGap
                Repeater {
                    model: [qsTr("Только русский"), qsTr("с пунктуацией"), qsTr("MIT"), qsTr("Сбер"), qsTr("отечественная")]
                    RowLayout {
                        spacing: root.footerGap
                        Dot { visible: index !== 0 }
                        FooterText { text: modelData }
                    }
                }
            }
            Item {
                width: Math.max(0, tagsRow.width - tags.width - diskSize.width - 2 * root.footerGap)
                height: diskSize.height
            }
            Row {
                id: diskSize
                FooterText {
                    text: root.modelSize
                    font.family: Theme.fontMono
                }
                FooterText { text: qsTr(" на диске") }
            }
        }

        RowLayout {
            width: parent.width
            visible: root.failed || root.warning || root.offline
            spacing: root.footerGap
            Icon {
                name: root.offline ? "globe" : "alert"
                size: 15 // spec §5.4 / макет: иконка сообщения об ошибке.
                color: statusText.color
            }
            FooterText {
                id: statusText
                Layout.fillWidth: true
                color: root.failed ? Theme.dangerInk : (root.warning ? Theme.warningInk : Theme.fgMuted)
                text: root.modelState === "broken" ? qsTr("Файл не прошёл проверку — скачайте заново")
                    : root.modelState === "no-space" ? qsTr("Не хватает места на диске — нужно ещё")
                    : root.warning ? qsTr("Памяти может не хватить — модели нужно около")
                    : qsTr("Нет доступа к %1").arg(root.modelHost)
            }
            FooterText {
                visible: root.modelState === "no-space" || root.warning
                text: root.warning ? qsTr("415 МБ") : root.modelSize
                font.family: Theme.fontMono
                color: statusText.color
            }
        }

        RowLayout {
            id: actions
            width: parent.width
            visible: !root.busy
            spacing: root.footerGap
            Item {
                Layout.fillWidth: true
            }
            FooterText {
                visible: root.available || root.warning
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
                text: qsTr("Источник: %1").arg(root.modelHost)
            }
            SmallButton {
                visible: root.available || root.warning || root.offline
                variant: root.warning ? "secondary" : "primary"
                iconName: "down"
                enabled: !root.offline
                text: qsTr("Скачать %1").arg(root.modelSize)
                onClicked: root.downloadRequested()
            }
            AvButton {
                id: fileButton
                Layout.minimumWidth: implicitWidth
                visible: root.available || root.warning || root.offline
                small: true
                iconName: "folder"
                text: root.offline ? qsTr("Установить из файла…") : qsTr("Из файла…")
                onClicked: root.installFromPathRequested()
            }
            SmallButton {
                id: installedButton
                visible: root.installed
                enabled: false
                text: qsTr("Установлена · %1").arg(root.modelSize)
            }
            AvButton {
                id: retryButton
                Layout.minimumWidth: implicitWidth
                visible: root.failed
                small: true
                text: qsTr("Повторить")
                onClicked: root.retryRequested()
            }
            AvButton {
                id: recoveryButton
                Layout.minimumWidth: implicitWidth
                visible: root.failed
                small: true
                text: root.modelState === "broken" ? qsTr("Удалить загрузку") : qsTr("Открыть папку")
                onClicked: {
                    if (root.modelState === "broken") root.removeDownloadRequested();
                    else root.openFolderRequested();
                }
            }
        }
    }
}
