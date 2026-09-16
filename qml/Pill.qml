// Содержимое пилюли — design/spec.md §8.1–§8.4. Окном и сроками состояний управляет Python.
import QtQuick 2.15
import "."
import "components"

Item {
    id: root

    // Значения, которых нет в PillTheme; остальные размеры и цвета — только из него.
    readonly property real paddingX: 10 // design/spec.md §8.2: паддинг 0 10.
    readonly property real paddingY: 0 // design/spec.md §8.2: паддинг 0 10.
    readonly property real textSize: 12.5 // design/spec.md §8.2: кегль подписи.
    readonly property string textFamily: "PT Root UI" // design/spec.md §8.2: гарнитура UI референса.
    readonly property int keywordWeight: Font.Bold // design/spec.md §8.2: CSS 700 = Qt Font.Bold.
    readonly property real iconSmall: 15 // design/spec.md §8.4: alert, clock, file, x, «—».
    readonly property real iconLarge: 16 // design/spec.md §8.4: refresh, check, alert ошибки.
    readonly property real closeSize: 22 // design/spec.md §8.2: кнопка 22 × 22.
    readonly property real closeGlyphSize: 13 // design/mockups/directions/_base.py .pill .x: кегль знака.
    readonly property real dotWidth: 5 // design/spec.md §8.4: ширина точек processing.
    readonly property var dotHeights: [5, 9, 5] // design/spec.md §8.4: три точки, высоты 5/9/5.
    readonly property real pulseOpacity: 0.5 // design/spec.md §8.4: пульсация; минимум выбран без гашения.
    readonly property int pulseDuration: 500 // design/spec.md §8.4: полупериод пульсации (темп не задан).
    readonly property real shadowX: 0 // design/spec.md §8.2: тень 0 6px 18px.
    readonly property real shadowY: 6 // design/spec.md §8.2: тень 0 6px 18px.
    readonly property real shadowRadius: 18 // design/spec.md §8.2: размытие тени.
    readonly property color shadowColor: Qt.rgba(0, 0, 0, 0.4) // design/spec.md §8.2: цвет тени.
    readonly property int enterDuration: 160 // design/spec.md «Сквозные правила», п.4: появление.
    readonly property int exitDuration: 120 // design/spec.md «Сквозные правила», п.4: исчезновение.
    readonly property var enterCurve: [0.2, 0, 0, 1, 1, 1] // design/spec.md «Сквозные правила», п.4.
    readonly property var exitCurve: [0.4, 0, 1, 1, 1, 1] // design/spec.md «Сквозные правила», п.4.
    readonly property int spinnerDuration: 1000 // design/spec.md §8.4, У9: один оборот в секунду.
    readonly property real spinnerTurn: 360 // design/spec.md §8.4, У9: полный оборот в градусах.

    property string avState: "hidden"
    property string label: ""
    property var levels: []
    // Только для детерминированной съёмки в тестах; в программе анимации включены.
    property bool freezeAnimations: false

    signal cancelClicked()
    signal detailsClicked()

    readonly property real pillWidth: Math.max(PillTheme.pillMinW,
        Math.min(PillTheme.pillMaxW, content.width + paddingX * 2))
    readonly property real pillHeight: PillTheme.pillH
    readonly property bool active: avState !== "hidden" && avState !== "disabled"
    readonly property bool showBars: presentation.name === "listening"
        || presentation.name === "listening-silent" || presentation.name === "limit"
    readonly property bool silent: presentation.name === "listening-silent"
    readonly property bool processing: presentation.name === "processing"
    readonly property bool loading: presentation.name === "loading-model"
    readonly property bool error: presentation.name === "error"
    readonly property string iconName: {
        switch (presentation.name) {
        case "loading-model": return "refresh";
        case "listening-silent": return "alert";
        case "limit": return "clock";
        case "done": return "check";
        case "clipboard-only": return "file";
        case "cancelled": return "x";
        case "error": return "alert";
        default: return "";
        }
    }
    readonly property string defaultLabel: {
        switch (presentation.name) {
        case "loading-model": return qsTr("Загружаю модель…");
        case "listening": return qsTr("Слушаю");
        case "listening-silent": return qsTr("Микрофон молчит");
        case "limit": return qsTr("Достигнут лимит записи");
        case "processing": return qsTr("Распознаю…");
        case "done": return qsTr("Готово");
        case "clipboard-only": return qsTr("Скопировано в буфер");
        case "empty": return qsTr("Ничего не распознано");
        case "cancelled": return qsTr("Отменено");
        default: return "";
        }
    }

    implicitWidth: pillWidth
    implicitHeight: pillHeight
    width: pillWidth
    height: pillHeight
    visible: active || opacity > 0
    enabled: active
    opacity: active ? 1 : 0

    // Сохраняем содержимое на время ухода; это только переход видимости, без таймеров состояний.
    QtObject {
        id: presentation
        property string name: "hidden"
        property string label: ""
    }

    function updatePresentation() {
        if (avState !== "hidden" && avState !== "disabled") {
            presentation.name = avState;
            presentation.label = label;
        } else { presentation.label = ""; }
    }

    onAvStateChanged: updatePresentation()
    onLabelChanged: updatePresentation()
    Component.onCompleted: updatePresentation()

    // Python вызывает синхронно перед чтением pillWidth/pillHeight и показом окна.
    function forceLayout() {
        caption.forceLayout();
        content.forceLayout();
    }

    Behavior on opacity {
        id: visibilityTransition
        NumberAnimation {
            duration: visibilityTransition.targetValue ? root.enterDuration : root.exitDuration
            easing.type: Easing.Bezier
            easing.bezierCurve: visibilityTransition.targetValue ? root.enterCurve : root.exitCurve
        }
    }

    // Подложка на QImage работает и с software backend, без запроса GL-контекста.
    // MouseArea охватывают лишь кнопки; поля тени не участвуют в обработке мыши.
    Canvas {
        x: -root.shadowRadius
        y: -root.shadowRadius
        width: root.pillWidth + root.shadowRadius * 2
        height: root.pillHeight + root.shadowRadius * 2 + root.shadowY
        renderTarget: Canvas.Image
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            ctx.reset();
            ctx.clearRect(0, 0, width, height);
            var left = root.shadowRadius;
            var top = root.shadowRadius;
            var right = left + root.pillWidth;
            var bottom = top + root.pillHeight;
            var radius = PillTheme.pillRadius;
            ctx.beginPath();
            ctx.moveTo(left + radius, top);
            ctx.lineTo(right - radius, top);
            ctx.arcTo(right, top, right, top + radius, radius);
            ctx.lineTo(right, bottom - radius);
            ctx.arcTo(right, bottom, right - radius, bottom, radius);
            ctx.lineTo(left + radius, bottom);
            ctx.arcTo(left, bottom, left, bottom - radius, radius);
            ctx.lineTo(left, top + radius);
            ctx.arcTo(left, top, left + radius, top, radius);
            ctx.closePath();
            ctx.shadowOffsetX = root.shadowX;
            ctx.shadowOffsetY = root.shadowY;
            ctx.shadowBlur = root.shadowRadius;
            ctx.shadowColor = root.shadowColor;
            ctx.fillStyle = PillTheme.pillFg;
            ctx.fill();
            // Убираем источник и внутреннюю часть тени, как у внешней CSS box-shadow.
            ctx.shadowColor = "transparent";
            ctx.globalCompositeOperation = "destination-out";
            ctx.fill();
        }
    }

    Rectangle {
        id: background
        // Корневой Item следует размеру окна с полями; рисунок — размеру контента.
        width: root.pillWidth
        height: root.pillHeight
        radius: PillTheme.pillRadius
        color: PillTheme.pillBg
        antialiasing: true
    }

    // Ширина подписи — contentWidth, без переноса и обрезания даже при достижении maxW.
    // Дети Row центрируются через y: позиционер управляет только их x.
    Row {
        id: content
        x: (root.pillWidth - width) / 2
        y: root.paddingY
        height: root.pillHeight - root.paddingY * 2
        spacing: PillTheme.pillGap

        Item {
            id: bars
            visible: root.showBars
            width: PillTheme.pillBarsBlockW
            height: PillTheme.pillBarAreaH
            y: (content.height - height) / 2

            Repeater {
                model: PillTheme.pillBars

                Rectangle {
                    required property int index
                    readonly property real level: root.levels && root.levels[index] !== undefined
                        ? Number(root.levels[index]) || 0 : 0

                    x: index * (PillTheme.pillBarW + PillTheme.pillBarGap)
                    y: bars.height - height // Макет design/mockups/final/09-pill.html: .pill .lv { align-items: flex-end }.
                    width: PillTheme.pillBarW
                    // §8.3: напрямую по кадрам, без интерполяции или сглаживания истории.
                    height: root.silent || presentation.name === "limit" ? PillTheme.pillBarHFlat
                        : Math.max(PillTheme.pillBarHMin, Math.round(level * PillTheme.pillBarHMax))
                    radius: PillTheme.pillBarRadius
                    color: root.silent || presentation.name === "limit"
                        ? PillTheme.pillBarFlat : PillTheme.pillBarLive
                    antialiasing: true
                }
            }
        }

        Item {
            id: dots
            visible: root.processing
            width: root.dotHeights.length * root.dotWidth
                + (root.dotHeights.length - 1) * PillTheme.pillBarGap
            height: PillTheme.pillBarAreaH
            y: (content.height - height) / 2

            Repeater {
                model: root.dotHeights

                Rectangle {
                    id: dot
                    required property int index
                    required property real modelData

                    x: index * (root.dotWidth + PillTheme.pillBarGap)
                    y: (dots.height - height) / 2
                    width: root.dotWidth
                    height: modelData
                    radius: width / 2
                    color: PillTheme.pillProcessing
                    antialiasing: true

                    SequentialAnimation on opacity {
                        running: root.active && root.visible && root.processing
                            && !root.freezeAnimations
                        loops: Animation.Infinite
                        onStopped: if (root.freezeAnimations) dot.opacity = 1
                        NumberAnimation {
                            from: 1
                            to: root.pulseOpacity
                            duration: root.pulseDuration
                            easing.type: Easing.Linear
                        }
                        NumberAnimation {
                            from: root.pulseOpacity
                            to: 1
                            duration: root.pulseDuration
                            easing.type: Easing.Linear
                        }
                    }
                }
            }
        }

        Icon {
            id: glyph
            visible: root.iconName !== ""
            name: root.iconName
            size: root.loading || root.error || presentation.name === "done"
                ? root.iconLarge : root.iconSmall
            y: (content.height - height) / 2
            color: root.error ? PillTheme.pillError
                : root.silent ? PillTheme.pillWarning
                : presentation.name === "done" ? PillTheme.pillDone
                : presentation.name === "cancelled" ? PillTheme.pillIconNeutral
                : PillTheme.pillIconMuted

            RotationAnimator {
                target: glyph
                from: 0
                to: root.spinnerTurn
                duration: root.spinnerDuration
                easing.type: Easing.Linear
                loops: Animation.Infinite
                running: root.active && root.visible && root.loading && !root.freezeAnimations
                // Общий глиф после спиннера должен снова быть повёрнут на ноль градусов.
                onStopped: glyph.rotation = 0
            }
        }

        Text {
            visible: presentation.name === "empty"
            text: "—"
            font.family: root.textFamily
            font.pixelSize: root.iconSmall
            color: PillTheme.pillIconNeutral
            y: (content.height - height) / 2
            renderType: Text.NativeRendering
        }

        Text {
            id: caption
            text: presentation.label !== "" ? presentation.label : root.defaultLabel
            textFormat: Text.PlainText
            width: contentWidth
            y: (content.height - height) / 2
            font.family: root.textFamily
            font.pixelSize: root.textSize
            font.weight: presentation.name === "listening" || presentation.name === "done"
                ? root.keywordWeight : Font.Normal
            color: root.error ? PillTheme.pillError : PillTheme.pillFg
            renderType: Text.NativeRendering
        }

        Rectangle {
            visible: presentation.name === "listening" || presentation.name === "listening-silent"
            width: root.closeSize
            height: root.closeSize
            y: (content.height - height) / 2
            radius: width / 2
            color: PillTheme.pillCloseBg
            antialiasing: true

            Icon {
                anchors.centerIn: parent
                name: "x"
                size: root.iconSmall
                color: PillTheme.pillCloseFg
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.cancelClicked()
            }
        }

        Rectangle {
            visible: root.error
            width: root.closeSize
            height: root.closeSize
            y: (content.height - height) / 2
            radius: width / 2
            color: PillTheme.pillCloseBg
            antialiasing: true

            Text {
                anchors.centerIn: parent
                text: "›"
                font.family: root.textFamily
                font.pixelSize: root.closeGlyphSize
                color: PillTheme.pillCloseFg
                renderType: Text.NativeRendering
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.detailsClicked()
            }
        }
    }
}
