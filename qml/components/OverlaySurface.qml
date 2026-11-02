// Shared software-rendered overlay surface; dimensions exclude shadow margins.
import QtQuick 2.15
import ".."

Item {
    id: root
    property real surfaceWidth: width
    property real surfaceHeight: height
    property real surfaceRadius: PillTheme.pillRadius
    property real shadowX: 0
    property real shadowY: 6
    property real shadowRadius: 18
    property color shadowColor: Qt.rgba(0, 0, 0, 0.4)
    Canvas {
        x: -root.shadowRadius
        y: -root.shadowRadius
        width: root.surfaceWidth + root.shadowRadius * 2
        height: root.surfaceHeight + root.shadowRadius * 2 + root.shadowY
        renderTarget: Canvas.Image
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            ctx.reset();
            ctx.clearRect(0, 0, width, height);
            var left = root.shadowRadius;
            var top = root.shadowRadius;
            var right = left + root.surfaceWidth;
            var bottom = top + root.surfaceHeight;
            var radius = root.surfaceRadius;
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
        width: root.surfaceWidth
        height: root.surfaceHeight
        radius: root.surfaceRadius
        color: PillTheme.pillBg
        antialiasing: true
    }
}
