// Completion must establish that every requested resource rendered within the pixel budget.
var mafFailure = null;
var mafPainting = 0;
var mafGetStencil = mxStencilRegistry.getStencil;
mxStencilRegistry.getStencil = function(name) {
    var stencil = mafGetStencil.apply(this, arguments);
    if (mafPainting > 0 && name && !stencil) {
        mafFailure = 'Unsupported resource stencil';
    }
    return stencil;
};
var mafIndicatorImage = mxGraph.prototype.getIndicatorImage;
mxGraph.prototype.getIndicatorImage = function(state) {
    var image = mafIndicatorImage.apply(this, arguments);
    // Semicolons delimit styles; restore the validated image's data URI marker.
    return image == null ? image : image.replace(/^(data:image\/(?:png|jpeg|svg\+xml)),/, '$1;base64,');
};
var mafCreateShape = mxCellRenderer.prototype.createShape;
mxCellRenderer.prototype.createShape = function(state) {
    var shape = state.style[mxConstants.STYLE_SHAPE];
    if (shape && !mxStencilRegistry.getStencil(shape) && !mxCellRenderer.defaultShapes[shape]) {
        mafFailure = 'Unsupported shape';
    }
    var indicator = state.style[mxConstants.STYLE_INDICATOR_SHAPE];
    // Indicator construction uses defaultShapes only, unlike the main shape.
    if (indicator && !mxCellRenderer.defaultShapes[indicator]) {
        mafFailure = 'Unsupported indicator shape';
    }
    var created = mafCreateShape.apply(this, arguments);
    if (created != null) {
        var paint = created.paint;
        created.paint = function() {
            mafPainting++;
            try {
                return paint.apply(this, arguments);
            } finally {
                mafPainting--;
            }
        };
    }
    return created;
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
