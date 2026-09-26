// Обрамление пяти шагов — design/spec.md §10, клавиатура — §13.1.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property bool freezeAnimations: false
    readonly property int totalSteps: root.bridge ? root.bridge.totalSteps : Theme.onboardingSteps
    readonly property int step: Math.max(1, Math.min(root.totalSteps, root.bridge ? root.bridge.step : 1))
    readonly property string barHint: loader.item && loader.item.barHint !== undefined ? loader.item.barHint : ""
    readonly property bool skipEnabled: loader.item && loader.item.skipEnabled !== undefined ? loader.item.skipEnabled : true
    readonly property bool canFinish: root.bridge ? root.bridge.canFinish : false
    readonly property var stepNames: [qsTr("Сеть"), qsTr("Модель"), qsTr("Горячая клавиша"), qsTr("Микрофон"), qsTr("Готово")]
    readonly property var stepSources: ["Step1Network.qml", "Step2Model.qml", "Step3Hotkey.qml", "Step4Mic.qml", "Step5Done.qml"]

    anchors.fill: parent
    implicitWidth: Theme.sizeWindowW
    implicitHeight: Theme.sizeWindowH - Theme.sizeTitlebarH
    focus: true

    function advance() {
        if (!continueButton.enabled)
            return
        if (root.step === root.totalSteps) {
            if (root.bridge)
                root.bridge.finish()
        } else if (root.bridge) {
            if (root.step === 2)
                root.bridge.startSelectedDownloads()
            root.bridge.next()
        }
    }

    Keys.onEscapePressed: {
        event.accepted = false
        if (loader.item && loader.item.escPressed)
            event.accepted = loader.item.escPressed() === true
    }

    // Shortcut обрабатывает Enter и когда фокус находится на контроле тела/панели.
    Shortcut {
        sequence: "Return"
        enabled: root.visible && root.enabled
        onActivated: root.advance()
    }

    Shortcut {
        sequence: "Enter"
        enabled: root.visible && root.enabled
        onActivated: root.advance()
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.bgApp
    }

    RowLayout {
        id: header
        x: Theme.spaceWindowContentX
        y: 14 // spec §10: верхний отступ шапки.
        width: Math.max(0, root.width - Theme.spaceWindowContentX * 2)
        spacing: 12 // spec §10: зазор элементов шапки.

        Text {
            Layout.alignment: Qt.AlignVCenter
            textFormat: Text.PlainText
            text: qsTr("Шаг %1 из %2").arg(root.step).arg(root.totalSteps)
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaptionSize
            lineHeight: Theme.fontCaptionSize * Theme.fontCaptionLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }

        Row {
            Layout.alignment: Qt.AlignVCenter
            spacing: Theme.onboardingDotsGap

            Repeater {
                model: root.totalSteps

                Rectangle {
                    required property int index
                    width: index + 1 === root.step ? Theme.onboardingDotActiveW : Theme.onboardingDot
                    height: Theme.onboardingDot
                    radius: index + 1 === root.step ? Theme.onboardingDotActiveRadius : height / 2
                    color: index + 1 === root.step ? Theme.primary
                        : index + 1 < root.step ? Theme.fgFaint : Theme.border
                    antialiasing: true
                }
            }
        }

        Item { Layout.fillWidth: true }

        Text {
            Layout.alignment: Qt.AlignVCenter
            textFormat: Text.PlainText
            text: root.stepNames[root.step - 1]
            color: Theme.fgMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaptionSize
            renderType: Text.NativeRendering
        }
    }

    // Объявление тела перед панелью задаёт порядок Tab: контент → кнопки панели.
    Item {
        id: body
        anchors.top: header.bottom
        anchors.bottom: dlStrip.visible ? dlStrip.top : bar.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 40 // spec §10: левый паддинг тела.
        anchors.rightMargin: 40 // spec §10: правый паддинг тела.
        clip: true

        Loader {
            id: loader
            objectName: "stepLoader"
            // Окно минус (sizeWindowW − onboardingStep2ContentW) = 228 px;
            // реальная ширина тела остаётся верхней границей.
            readonly property real step2AvailableWidth: Math.max(0, Math.min(body.width,
                root.width - (Theme.sizeWindowW - Theme.onboardingStep2ContentW)))
            y: 24 // design/spec.md §10: верхний паддинг тела, отдельного токена нет.
            anchors.horizontalCenter: parent.horizontalCenter
            source: root.stepSources[root.step - 1]
            focus: true
        }
    }

    DownloadStrip {
        id: dlStrip
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: bar.top
        downloadState: (root.step >= 2 && root.bridge) ? root.bridge.downloadState : "idle"
        title: root.bridge ? root.bridge.downloadTitle : ""
        downloadCounter: root.bridge ? root.bridge.downloadCounter : ""
        sourceText: root.bridge && root.bridge.downloadSource !== undefined
            ? root.bridge.downloadSource : ""
        progress: root.bridge ? root.bridge.downloadProgress : 0
        speed: root.bridge ? root.bridge.speed : ""
        eta: root.bridge ? root.bridge.eta : ""
        detail: root.bridge ? root.bridge.downloadDetail : ""
        freezeAnimations: root.freezeAnimations
        onRetryRequested: { if (root.bridge) root.bridge.startSelectedDownloads(); }
        onOpenFolderRequested: { if (root.bridge) root.bridge.openModelsFolder(); }
    }

    Rectangle {
        id: bar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: Theme.onboardingBarH
        color: Theme.bgApp

        Rectangle {
            width: parent.width
            height: Theme.borderDivider
            color: Theme.border
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.spaceWindowContentX
            anchors.rightMargin: Theme.spaceWindowContentX
            spacing: 10 // spec §10: зазор элементов нижней панели.

            AvButton {
                text: qsTr("Назад")
                variant: "secondary"
                visible: root.step > 1
                Layout.alignment: Qt.AlignVCenter
                onClicked: if (root.bridge && root.bridge.back) root.bridge.back()
            }

            Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: bar.width / 2
                textFormat: Text.PlainText
                text: root.barHint
                visible: text !== ""
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }

            Item { Layout.fillWidth: true }

            AvButton {
                text: qsTr("Пропустить")
                variant: "ghost"
                visible: root.step < root.totalSteps
                enabled: root.skipEnabled
                Layout.alignment: Qt.AlignVCenter
                onClicked: if (root.bridge && root.bridge.skip) root.bridge.skip()
            }

            AvButton {
                id: continueButton
                text: root.step === root.totalSteps ? qsTr("Готово") : qsTr("Продолжить")
                variant: "primary"
                enabled: root.step === root.totalSteps ? root.canFinish
                    : root.step === 2 ? (root.bridge ? root.bridge.canContinueFromModel : false) : true
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.advance()
            }
        }
    }
}
