// Completion must establish that every requested resource rendered within the pixel budget.
var mafFailure = null;
var mafCreateShape = mxCellRenderer.prototype.createShape;
mxCellRenderer.prototype.createShape = function(state) {
    var shape = state.style[mxConstants.STYLE_SHAPE];
    if (shape && !mxStencilRegistry.getStencil(shape) && !mxCellRenderer.defaultShapes[shape]) {
        mafFailure = 'Unsupported shape';
    }
    var icon = state.style.resIcon;
    if (icon && !mxStencilRegistry.getStencil(icon) && !mxCellRenderer.defaultShapes[icon]) {
        mafFailure = 'Unsupported resource icon';
    }
    return mafCreateShape.apply(this, arguments);
};
function mafSend(channel, value) {
    if (channel === 'render-finished' && value != null) {
        var bounds = JSON.parse(value.bounds);
        var width = Math.ceil(bounds.width + Math.max(0, bounds.x)) + 1;
        var height = Math.ceil(bounds.height + Math.max(0, bounds.y)) + 1;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0 ||
            width > 4096 || height > 4096 || width * height > 16000000) {
            mafFailure = 'Export exceeds the dimension or pixel limit';
        }
    }
    if (mafFailure && (channel === 'render-finished' || channel === 'svg-data')) {
        electron.sendMessage('export-error', 'MAF_REFUSED: ' + mafFailure);
    } else {
        electron.sendMessage(channel, value);
    }
}
