// Строка-статус — design/spec.md §2. Высота 36, фон bg-app, граница сверху 1 px.
// Слева: иконка состояния + активная модель. Справа: состояние обновления · точка · версия (PT Mono).
// В M1 живёт одно состояние справа — `disabled` (проверка обновлений отключена), серым, без клика.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import "components"

Item {
    id: root

    // Левая часть (§2.1): loading | active | switching | error | none.
    property string modelState: "active"
    property string modelName: "GigaAM v3 RNN-T"
    // Правая часть (§2.2). В M1 реализовано состояние 1 — `disabled`.
    property string updateState: "disabled"
    property string version: "v0.2.0"

    readonly property string modelText: {
        switch (modelState) {
        case "loading": return qsTr("Загружаю %1…").arg(modelName);
        case "switching": return qsTr("Переключаю на %1…").arg(modelName);
        case "error": return qsTr("Модель не загрузилась — открыть Модели");
        case "none": return qsTr("Модель не выбрана — установить");
        default: return modelName;
        }
    }

    readonly property string updateText: {
        switch (updateState) {
        case "checking": return qsTr("Проверяю обновления…");
        default: return qsTr("Проверка обновлений отключена");
        }
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
        anchors.fill: parent
        anchors.leftMargin: Theme.cardRowPaddingX
        anchors.rightMargin: Theme.cardRowPaddingX

        RowLayout {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: 7  // §2: зазор иконки трея до текста

            BrandMark {
                size: Theme.statusbarTrayIcon
                tray: true  // иконка состояния — мастер-геометрия 22 (§9.1), не логотип
                color: Theme.statusbarFg
                Layout.alignment: Qt.AlignVCenter
            }

            Text {
                text: root.modelText
                color: Theme.statusbarFg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontStatusbarSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }
        }

        RowLayout {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Theme.statusbarGap

            Text {
                text: root.updateText
                color: Theme.statusbarFg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontStatusbarSize
                renderType: Text.NativeRendering
                Layout.alignment: Qt.AlignVCenter
            }

            // Точка-разделитель 4 px с полями 8 (§2).
            Item {
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
