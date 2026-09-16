// design/mockups/final/08-onboarding-2-model.html
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property string barHint: qsTr("Пропустить нельзя: без модели программа не работает")
    property bool skipEnabled: false

    implicitWidth: 620 // Макет 08-onboarding-2-model.html: ширина содержимого.
    implicitHeight: fileRow.y + fileRow.height
    width: implicitWidth
    height: implicitHeight

    Text {
        id: heading
        width: root.width
        text: qsTr("Выберите модель распознавания")
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
        y: heading.height + 4 // Макет 08-onboarding-2-model.html: отступ подзаголовка.
        width: root.width
        text: qsTr("Без модели диктовка не работает — это единственный шаг, который нельзя пропустить. Рекомендуем русскую GigaAM: она расставляет знаки препинания сама.")
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmallSize
        lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.WordWrap
    }

    OnboardingModelCard {
        id: card
        y: subtitle.y + subtitle.height + 14 // Макет 08-onboarding-2-model.html: нижний отступ подзаголовка.
        width: root.width
        modelState: root.bridge ? root.bridge.modelState : "downloadable"
        modelName: root.bridge && root.bridge.modelName ? root.bridge.modelName : qsTr("GigaAM v3 RNN-T")
        modelSize: root.bridge && root.bridge.modelSize ? root.bridge.modelSize : qsTr("231,9 МБ")
        modelRam: root.bridge && root.bridge.modelRam ? root.bridge.modelRam : ""
        modelHost: root.bridge && root.bridge.modelHost ? root.bridge.modelHost : qsTr("huggingface.co")
        modelMessage: root.bridge ? root.bridge.modelMessage : ""
        progress: root.bridge ? root.bridge.progress : 0
        speed: root.bridge ? root.bridge.speed : ""
        eta: root.bridge ? root.bridge.eta : ""
        onDownloadRequested: { if (root.bridge && root.bridge.download) root.bridge.download(); }
        onCancelRequested: { if (root.bridge && root.bridge.cancelDownload) root.bridge.cancelDownload(); }
        onInstallFromPathRequested: { if (root.bridge) root.bridge.pickInstallPath(); }
        onRetryRequested: { if (root.bridge && root.bridge.download) root.bridge.download(); }
        onRemoveDownloadRequested: { if (root.bridge && root.bridge.cancelDownload) root.bridge.cancelDownload(); }
        onOpenFolderRequested: { /* слота в контракте пока нет */ }
    }

    RowLayout {
        id: fileRow
        y: card.y + card.height + 4 // Макет 08-onboarding-2-model.html: отступ ряда под карточкой.
        width: root.width
        Item { Layout.fillWidth: true }
        AvButton {
            iconName: "folder"
            text: qsTr("Установить из файла или папки…")
            onClicked: { if (root.bridge) root.bridge.pickInstallPath(); }
        }
    }
}
