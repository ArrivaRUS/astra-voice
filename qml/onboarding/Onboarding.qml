// Обрамление пяти шагов — design/spec.md §10, клавиатура — §13.1.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    readonly property int step: Math.max(1, Math.min(Theme.onboardingSteps, bridge ? bridge.step : 1))
    readonly property string barHint: loader.item && loader.item.barHint !== undefined ? loader.item.barHint : ""
    readonly property bool skipEnabled: loader.item && loader.item.skipEnabled !== undefined ? loader.item.skipEnabled : true
    readonly property bool canFinish: bridge ? bridge.canFinish : false
    readonly property var stepNames: [qsTr("Сеть"), qsTr("Модель"), qsTr("Горячая клавиша"), qsTr("Микрофон"), qsTr("Готово")]
    readonly property var stepSources: ["Step1Network.qml", "Step2Model.qml", "Step3Hotkey.qml", "Step4Mic.qml", "Step5Done.qml"]

    anchors.fill: parent
    implicitWidth: Theme.sizeWindowW
    implicitHeight: Theme.sizeWindowH - Theme.sizeTitlebarH
    focus: true

    function advance() {
        if (!continueButton.enabled)
            return
        if (step === Theme.onboardingSteps) {
            if (bridge && bridge.finish)
                bridge.finish()
        } else if (bridge && bridge.next) {
            bridge.next()
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
            text: qsTr("Шаг %1 из %2").arg(root.step).arg(Theme.onboardingSteps)
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
                model: Theme.onboardingSteps

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
        anchors.bottom: bar.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 40 // spec §10: левый паддинг тела.
        anchors.rightMargin: 40 // spec §10: правый паддинг тела.
        clip: true

        Loader {
            id: loader
            y: 24 // design/spec.md §10: верхний паддинг тела, отдельного токена нет.
            anchors.horizontalCenter: parent.horizontalCenter
            source: root.stepSources[root.step - 1]
            focus: true
        }
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
                visible: root.step < Theme.onboardingSteps
                enabled: root.skipEnabled
                Layout.alignment: Qt.AlignVCenter
                onClicked: if (root.bridge && root.bridge.skip) root.bridge.skip()
            }

            Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: bar.width / 2
                text: qsTr("Сначала установите модель — без неё диктовка не работает")
                visible: root.step === Theme.onboardingSteps && !root.canFinish
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaptionSize
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }

            AvButton {
                id: continueButton
                text: root.step === Theme.onboardingSteps ? qsTr("Готово") : qsTr("Продолжить")
                variant: "primary"
                enabled: root.step < Theme.onboardingSteps || root.canFinish
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.advance()
            }
        }
    }
}
