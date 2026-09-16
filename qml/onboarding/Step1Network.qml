// design/spec.md §10; design/mockups/final/08-onboarding-1-network.html.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."
import "../components"

Item {
    id: root

    readonly property var bridge: (typeof onboarding !== "undefined" && onboarding !== null) ? onboarding : null
    property string barHint: qsTr("Изменить можно позже в «Сеть и обновления»")
    property bool skipEnabled: true

    implicitWidth: 580 // Макет 08-onboarding-1-network.html: ширина содержимого.
    implicitHeight: note.y + note.height
    width: implicitWidth
    height: implicitHeight

    RowLayout {
        id: heading
        width: root.width
        spacing: 14 // spec §10: зазор верхнего ряда.

        BrandMark {
            appicon: true
            size: 52 // Макет 08-onboarding-1-network.html: размер знака.
            Layout.preferredWidth: size
            Layout.preferredHeight: size
            Layout.alignment: Qt.AlignVCenter
            // Плитка шага 1 — знак приложения; его цвета одинаковы в обеих темах,
            // поэтому берутся из PillTheme (color.fixed), а не из Theme.
            tileColor: PillTheme.appiconBg
            color: PillTheme.appiconMark
            accentColor: PillTheme.appiconAccent
        }

        Column {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 3 // spec §10: зазор в колонке заголовка.

            Text {
                width: parent.width
                text: qsTr("Astra Voice")
                color: Theme.fg
                font.family: Theme.fontUi
                font.pixelSize: 24 // Макет 08-onboarding-1-network.html: переопределение .h1.
                font.weight: Font.Bold
                lineHeight: font.pixelSize * Theme.fontH1WindowLineHeight
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
            }

            Text {
                width: parent.width
                text: qsTr("Голосовой ввод для Astra Linux. Распознавание идёт на этом компьютере — записи никуда не отправляются.")
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmallSize
                lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
                wrapMode: Text.WordWrap
            }
        }
    }

    // Карточка SettingGroup без пустого CAPS-заголовка и его отступов.
    Rectangle {
        id: card
        y: heading.height + 18 // spec §10: отступ карточки под заголовком.
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
                width: parent.width
                divider: false
                showHint: false
                label: qsTr("Язык интерфейса")
                sub: qsTr("Определён по системной локали ru_RU")

                AvSelect {
                    Layout.preferredWidth: 200 // Макет 08-onboarding-1-network.html: список языка.
                    Layout.alignment: Qt.AlignVCenter
                    model: [qsTr("Русский")]
                }
            }

            SettingRow {
                width: parent.width
                showHint: false
                label: qsTr("Проверять обновления утилиты")
                sub: qsTr("Раз в сутки, github.com")
                toggle: appUpdates
                locked: root.bridge ? root.bridge.policyLocked : false
                lockedText: root.bridge ? root.bridge.policyLockedText : ""
                rowEnabled: !locked

                AvToggle {
                    id: appUpdates
                    Layout.alignment: Qt.AlignVCenter
                    checked: root.bridge ? root.bridge.checkAppUpdates : false
                    locked: root.bridge ? root.bridge.policyLocked : false
                    // SettingRow вызывает toggle(): учитываем также клик по всей строке.
                    onCheckedChanged: {
                        if (root.bridge && !locked && root.bridge.checkAppUpdates !== checked)
                            root.bridge.checkAppUpdates = checked
                    }
                }
            }

            SettingRow {
                width: parent.width
                showHint: false
                label: qsTr("Проверять обновления моделей")
                sub: qsTr("Раз в сутки, huggingface.co")
                toggle: modelUpdates
                locked: root.bridge ? root.bridge.policyLocked : false
                lockedText: root.bridge ? root.bridge.policyLockedText : ""
                rowEnabled: !locked

                AvToggle {
                    id: modelUpdates
                    Layout.alignment: Qt.AlignVCenter
                    checked: root.bridge ? root.bridge.checkModelUpdates : false
                    locked: root.bridge ? root.bridge.policyLocked : false
                    onCheckedChanged: {
                        if (root.bridge && !locked && root.bridge.checkModelUpdates !== checked)
                            root.bridge.checkModelUpdates = checked
                    }
                }
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

    NoteBanner {
        id: note
        y: card.y + card.height + 14 // spec §10: отступ баннера под карточкой.
        width: root.width
        variant: "info"
        iconName: "info" // design/spec.md §7; макет 08-onboarding-1-network.html: информационная иконка.
        title: qsTr("Пока оба переключателя выключены, программа не выходит в сеть")
        body: qsTr("Скачать модель на следующем шаге можно и без них — это ваше явное действие. Список хостов и способ выключить сеть совсем — в «О программе → Приватность».")
    }
}
