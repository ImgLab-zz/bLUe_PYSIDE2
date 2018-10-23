"""
This File is part of bLUe software.

Copyright (C) 2017  Bernard Virot <bernard.virot@libertysurf.fr>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as
published by the Free Software Foundation, version 3.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
Lesser General Lesser Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
import gc

import weakref

from os.path import isfile

from PySide2.QtCore import Qt, QDataStream, QFile, QIODevice, QSize, QPoint

import cv2
from copy import copy

from PySide2.QtGui import QTransform, QColor
from PySide2.QtWidgets import QApplication
from PySide2.QtGui import QPixmap, QImage, QPainter
from PySide2.QtCore import QRect

import exiftool

from colorManagement import icc, convertQImage
from bLUeGui.bLUeImage import QImageBuffer, ndarrayToQImage
from bLUeGui.dialog import dlgWarn
from time import time

from lutUtils import LUT3DIdentity
from utils import  baseSignal_bool, baseSignal_Int2, qColorToRGB

from versatileImg import vImage


class ColorSpace:
    notSpecified = -1; sRGB = 1

class mImage(vImage):
    """
    Multi-layer image : base class for editable images.
    A mImage holds a presentation layer
    for color management and a stack containing at least a
    background layer. All layers share the same metadata instance.
    To correctly render a mImage, widgets should override their
    paint event handler.
    """
    @classmethod
    def restoreMeta(cls, srcFile, destFile, defaultorientation=True, thumbfile=None):
        """
        # copy metadata from sidecar to image file. The sidecar is not removed.
        If defaultorientaion is True the orientaion of the destination file is
        set to "no change" (1). This way, saved images are shown as they were edited.
        @param srcFile: source image or sidecar (the extension is replaced by .mie).
        @type srcFile: str
        @param destFile: image file
        @type destFile: str
        @param defaultorientation
        @type defaultorientation: bool
        @param thumbnail: thumbnail data
        @type thumbnail: bytearray
        """
        with exiftool.ExifTool() as e:
            e.copySidecar(srcFile, destFile)
            if defaultorientation:
                e.writeOrientation(destFile, '1')
            if thumbfile is not None:
                    e.writeThumbnail(destFile, thumbfile)
    def __init__(self, *args, **kwargs):
        # as updatePixmap uses layersStack, it must be initialized
        # before the call to super(). __init__
        self.layersStack = []
        # link back to QLayerView window
        self.layerView = None
        super().__init__(*args, **kwargs)  # must be done before prLayer init.
        # add background layer
        bgLayer = QLayer.fromImage(self, parentImage=self)
        bgLayer.isClipping = True
        self.setModified(False)
        self.activeLayerIndex = None
        self.addLayer(bgLayer, name='Background')
        # color management : we assume that the image profile is the working profile
        self.colorTransformation = icc.workToMonTransform
        # presentation layer
        prLayer = QPresentationLayer(QImg=self, parentImage=self)
        prLayer.name = 'presentation'
        prLayer.role ='presentation'
        prLayer.execute = lambda l=prLayer, pool=None: prLayer.applyNone()
        prLayer.updatePixmap() # mandatory here as vImage init. can't do it
        self.prLayer = prLayer
        self.isModified = False
        # rawpy object
        self.rawImage = None

    def bTransformed(self, transformation):
        """
        Applies transformation to all layers in stack and returns
        the new mImage
        @param transformation:
        @type transformation: Qtransform
        @return:
        @rtype: mimage
        """
        img = mImage(QImg=self.transformed(transformation))
        img.meta = self.meta
        img.onImageChanged = self.onImageChanged
        img.useThumb = self.useThumb
        img.useHald = self.useHald
        stack = []
        for layer in self.layersStack:
            tLayer = layer.bTransformed(transformation, img)
            stack.append(tLayer)
        img.layersStack = stack
        gc.collect()
        return img

    def getActiveLayer(self):
        """
        Returns the currently active layer.
        @return: The active layer
        @rtype: QLayer
        """
        if self.activeLayerIndex is not None:
            return self.layersStack[self.activeLayerIndex]
        else:
            return None

    def setActiveLayer(self, stackIndex):
        """
        Assigns stackIndex value to  activeLayerIndex and
        updates the layer view and tools
        @param stackIndex: index in stack for the layer to select
        @type stackIndex: int
        @return: active layer
        @rtype: QLayer
        """
        lgStack = len(self.layersStack)
        if stackIndex < 0 or stackIndex >= lgStack:
            return
        # clean old active layer
        active = self.getActiveLayer()
        if active is not None:
            if active.tool is not None:
                active.tool.hideTool()
        # set new active layer
        self.activeLayerIndex = stackIndex
        if self.layerView is not None:
            self.layerView.selectRow(lgStack - 1 - stackIndex)
        active = self.getActiveLayer()
        if active.tool is not None and active.visible:
            active.tool.showTool()
        return active

    def getActivePixel(self,x, y, fromInputImg=True):
        """
        Reads the RGB colors of the pixel at (x, y) from the active layer.
        If fromInputImg is True (default), the pixel is taken from
        the input image, otherwise from the current image.
        Coordinates are relative to the full sized image.
        If (x,y) is outside the image, (0, 0, 0) is returned.
        @param x, y: coordinates of pixel, relative to the full-sized image
        @return: pixel RGB colors
        @rtype: 3-uple of int
        """
        x, y = self.full2CurrentXY(x, y)
        activeLayer = self.getActiveLayer()
        qClr = activeLayer.inputImg().pixelColor(x, y) if fromInputImg else activeLayer.getCurrentImage().pixelColor(x, y)
        # pixelColor returns an invalid color if (x,y) is out of range
        # we return black
        if not qClr.isValid():
            qClr = QColor(0,0,0)
        return qColorToRGB(qClr)

    def getPrPixel(self, x, y):
        """
        Reads the RGB colors of the pixel at (x, y) from
        the presentation layer. They are the (non color managed)
        colors of the displayed pixel.
        Coordinates are relative to the full sized image.
        If (x,y) is outside the image, (0, 0, 0) is returned.
        @param x, y: coordinates of pixel, relative to the full-sized image
        @return: pixel RGB colors
        @rtype: 3-uple of int
        """
        x, y = self.full2CurrentXY(x, y)
        qClr = self.prLayer.getCurrentImage().pixelColor(x, y)
        if not qClr.isValid():
            qClr = QColor(0,0,0)
        return qColorToRGB(qClr)

    def cacheInvalidate(self):
        """
        Invalidate cache buffers. The method is
        called by applyToStack for each layer after layer.execute
        """
        vImage.cacheInvalidate(self)
        for layer in self.layersStack:
            layer.cacheInvalidate()  # As Qlayer doesn't inherit from mImage, we call vImage.cacheInvalidate(layer)

    def setThumbMode(self, value):
        if value == self.useThumb:
            return
        self.useThumb = value
        # recalculate the whole stack
        self.layerStack[0].apply()

    def updatePixmap(self):
        """
        Updates all pixmaps
        Overrides vImage.updatePixmap()
        """
        vImage.updatePixmap(self)
        for layer in self.layersStack:
            vImage.updatePixmap(layer)
        self.prLayer.updatePixmap()

    def getStackIndex(self, layer):
        p = id(layer)
        i = -1
        for i,l in enumerate(self.layersStack):
            if id(l) == p:
                break
        return i

    def addLayer(self, layer, name='', index=None):
        """
        Adds a layer.

        @param layer: layer to add (fresh layer if None, type QLayer)
        @type layer: QLayer
        @param name:
        @type name: str
        @param index: index of insertion in layersStack (top of active layer if index=None)
        @type index: int
        @return: the layer added
        @rtype: QLayer
        """
        # build a unique name
        usedNames = [l.name for l in self.layersStack]
        a = 1
        trialname = name if len(name) > 0 else 'noname'
        while trialname in usedNames:
            trialname = name + '_'+ str(a)
            a = a+1
        if layer is None:
            layer = QLayer(QImg=self, parentImage=self) # TODO 11/09/18 added parentImage validate
            layer.fill(Qt.white)
        layer.name = trialname
        #layer.parentImage = self # TODO 07/06/18 validate suppression
        if index is None:
            if self.activeLayerIndex is not None:
                # add on top of active layer if any
                index = self.activeLayerIndex + 1 #TODO +1 added 03/05/18 validate
            else:
                # empty stack
                index = 0
        self.layersStack.insert(index, layer)
        self.setActiveLayer(index)
        layer.meta = self.meta
        layer.parentImage = self
        self.setModified(True)
        return layer

    def removeLayer(self, index=None):
        if index is None:
            return
        self.layersStack.pop(index)

    def addAdjustmentLayer(self, name='', role='',  index=None, sourceImg=None):
        """
        Adds an adjustment layer to the layer stack, at
        position index (default is top of active layer)
        @param name:
        @type name:
        @param role:
        @type role:
        @param index:
        @type index:
        @param sourceImg:
        @type sourceImg:
        @return:
        @rtype:
        """
        if index is None:
            # adding on top of active layer
            index = self.activeLayerIndex
        if sourceImg is None:
            # set image from active layer
            layer = QLayer.fromImage(self.layersStack[index], parentImage=self)
        else:
            # image layer :
            layer = QLayerImage.fromImage(self.layersStack[index], parentImage=self, sourceImg=sourceImg)
        layer.role = role
        self.addLayer(layer, name=name, index=index + 1)
        # add autoSpline attribute to contrast layer only
        if role in ['CONTRAST', 'RAW']:
            layer.autoSpline = True
        # init thumb
        if layer.parentImage.useThumb:
            layer.thumb = layer.inputImg().copy()
        group = self.layersStack[index].group
        if group:
            layer.group = group
            layer.mask = self.layersStack[index].mask
            layer.maskIsEnabled = True
        # sync caches
        layer.updatePixmap()
        return layer

    def addSegmentationLayer(self, name='', index=None):
        if index is None:
            index = self.activeLayerIndex
        layer = QLayer.fromImage(self.layersStack[index], parentImage=self)
        layer.role = 'SEGMENT'
        layer.inputImg = lambda: self.layersStack[layer.getLowerVisibleStackIndex()].getCurrentMaskedImage() # TODO 13/06/18 is it different from other layers?
        self.addLayer(layer, name=name, index=index + 1)
        layer.maskIsEnabled = True
        layer.maskIsSelected = True
        # mask pixels are not yet painted as FG or BG
        # so we mark them as invalid
        layer.mask.fill(vImage.defaultColor_Invalid)
        layer.paintedMask = layer.mask.copy()
        # add noSegment flag. It blocks/allows the execution of grabcut algorithm
        # in applyGrabcut : if True, further stack updates
        # do not redo the segmentation. The flag is toggled by the Apply Button
        # slot of segmentForm.
        layer.noSegment = False
        layer.updatePixmap()
        return layer

    def dupLayer(self, index=None):
        """
        inserts in layersStack at position index+1 a copy of the layer 
        at position index. If index is None (default value), the layer is inserted
        on top of the stack. Adjustment layers are not duplicated.
        @param index: 
        @type index: int
        @return: 
        """
        if index is None:
            index = len(self.layersStack) - 1
        layer0 = self.layersStack[index]
        if layer0.isAdjustLayer():
            return
        layer1 = QLayer.fromImage(layer0, parentImage=self)
        self.addLayer(layer1, name=layer0.name, index=index+1)

    def mergeVisibleLayers(self):
        """
        Merges the visible masked images and returns the
        resulting QImage, eventually scaled to fit the image size.
        @return: image
        @rtype: QImage
        """
        # init a new image
        img = QImage(self.width(), self.height(), self.format())
        # Image may contain transparent pixels, hence we
        # fill the image with a background color.
        img.fill(vImage.defaultBgColor)
        # draw layers with (eventually) masked areas.
        qp = QPainter(img)
        qp.drawImage(QRect(0, 0, self.width(), self.height()), self.layersStack[-1].getCurrentMaskedImage())
        qp.end()
        return img

    def save(self, filename, quality=-1, compression=-1, crops=None):
        """
        Overrides QImage.save().
        Writes the presentation layer to a file and returns a
        thumbnail with standard size (160x120 or 120x160).
        Raises IOError if the saving fails.
        @param filename:
        @type filename: str
        @param quality: integer value in range 0..100, or -1
        @type quality: int
        @return: thumbnail of the saved image
        @rtype: QImage
        """
        # don't save thumbnails
        if self.useThumb:
            return None
        # get the final image from the presentation layer.
        # This image is NOT color managed (prLayer.qPixmap
        # only is color managed)
        img = self.prLayer.getCurrentImage()
        # imagewriter and QImage.save are unusable for tif files,
        # due to bugs in libtiff, hence we use opencv imwrite.
        fileFormat = filename[-3:].upper()
        buf = QImageBuffer(img)
        if fileFormat == 'JPG':
            buf = buf[:, :, :3]
            params = [cv2.IMWRITE_JPEG_QUALITY, quality]  # quality range 0..100
        elif fileFormat == 'PNG':
            params = [cv2.IMWRITE_PNG_COMPRESSION, compression]  # compression range 0..9
        elif fileFormat == 'TIF':
            buf = buf[:, :, :3]
            params = []
        else:
            raise IOError("Invalid File Format\nValid formats are jpg, png, tif ")
        if self.isCropped:
            # make slices
            w, h = self.width(), self.height()
            w1, w2 = int(self.cropLeft), w - int(self.cropRight)
            h1, h2 = int(self.cropTop), h - int(self.cropBottom)
            buf = buf[h1:h2, w1:w2,:]
        # build thumbnail from (evenyually) cropped image
        # choose thumb size
        wf, hf = buf.shape[1], buf.shape[0]
        if wf > hf:
            wt, ht = 160, 120
        else:
            wt, ht = 120, 160
        thumb = ndarrayToQImage(np.ascontiguousarray(buf[:,:,:3][:,:,::-1]), format=QImage.Format_RGB888).scaled(wt,ht, Qt.KeepAspectRatio)
        #wr, hr = thumb.width(), thumb.height()
        #thumb = thumb.copy(QRect((wr-wt)//2,(hr-ht)//2, wt, ht))
        written = cv2.imwrite(filename, buf, params)  #BGR order
        if not written:
            raise IOError("Cannot write file %s " % filename)
        # self.setModified(False) # cannot be reset if the image is modified again
        return thumb

    def writeStackToStream(self, dataStream):
        dataStream.writeInt32(len(self.layersStack))
        for layer in self.layersStack:
            """
            dataStream.writeQString('menuLayer(None, "%s")' % layer.actionName)
            dataStream.writeQString('if "%s" != "actionNull":\n dataStream=window.label.img.layersStack[-1].readFromStream(dataStream)' % layer.actionName)
            """
            dataStream.writeQString(layer.actionName)
        for layer in self.layersStack:
            if getattr(layer, 'view', None) is not None:
                layer.view.widget().writeToStream(dataStream)

    def readStackFromStream(self, dataStream):
        # stack length
        count = dataStream.readInt32()
        script = []
        for i in range(count):
            actionName = dataStream.readQString()
            script.append('menuLayer(None, "%s")' % actionName)
            script.append('if "%s" != "actionNull":\n dataStream=window.label.img.layersStack[-1].readFromStream(dataStream)' % actionName)
        return script

    def saveStackToFile(self, filename):
        qf = QFile(filename)
        if not qf.open(QIODevice.WriteOnly):
            raise IOError('cannot open file %s' % filename)
        dataStream = QDataStream(qf)
        self.writeStackToStream(dataStream)
        qf.close()

    def loadStackFromFile(self, filename):
        qf = QFile(filename)
        if not qf.open(QIODevice.ReadOnly):
            raise IOError('cannot open file %s' % filename)
        dataStream = QDataStream(qf)
        script = self.readStackFromStream(dataStream)
        #qf.close()
        return script, qf, dataStream

class imImage(mImage) :
    """
    Zoomable and draggable multi-layer image :
    this is the base class for bLUe documents
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Zoom coeff :
        # Zoom_coeff = 1.0 displays an image fitting the
        # size of the current window ( NOT the actual pixels of the image).
        self.Zoom_coeff = 1.0
        self.xOffset, self.yOffset = 0, 0
        self.isMouseSelectable =True
        self.isModified = False

    def bTransformed(self, transformation):
        """
        Applies transformation to all layers in stack
        and returns the new imImage.
        @param transformation:
        @type transformation: QTransform
        @return:
        @rtype: imImage
        """
        img = imImage(QImg=self.transformed(transformation))
        img.meta = self.meta
        img.onImageChanged = self.onImageChanged
        img.useThumb = self.useThumb
        img.useHald = self.useHald
        stack = []
        # apply transformation to the stack. Note that
        # the presentation layer is automatically rebuilt
        for layer in self.layersStack:
            tLayer = layer.bTransformed(transformation, img)
            # keep ref to original layer (needed by execute)
            tLayer.parentLayer = layer.parentLayer
            # record new transformed layer in original layer (needed by execute)
            tLayer.parentLayer.tLayer = tLayer
            # link back grWindow to tLayer
            # using weak ref for back links
            if tLayer.view is not None:
                # for historical reasons, graphic forms inheriting
                # from QGraphicsView use form.scene().layer attribute,
                # others use form.layer
                # use weak refs for back links
                if type(tLayer) in weakref.ProxyTypes:
                    wtLayer = tLayer
                else:
                    wtLayer = weakref.proxy(tLayer)
                if getattr(tLayer.view.widget(), 'scene', None) is None:
                    tLayer.view.widget().layer = wtLayer
                else:
                    tLayer.view.widget().scene().layer = wtLayer
            stack.append(tLayer)
        img.layersStack = stack
        gc.collect()
        return img

    def resize(self, pixels, interpolation=cv2.INTER_CUBIC):
        """
        Resize image and layers
        @param pixels:
        @param interpolation:
        @return: resized imImage object
        @rtype: imImage
        """
        # resized vImage
        rszd0 = super().resize(pixels, interpolation=interpolation)
        # resized imImage
        rszd = imImage(QImg=rszd0,meta=copy(self.meta))
        rszd.rect = rszd0.rect
        for k, l  in enumerate(self.layersStack):
            if l.name != "background" and l.name != 'drawlayer':
                img = QLayer.fromImage(l.resize(pixels, interpolation=interpolation), parentImage=rszd)
                rszd.layersStack.append(img)
                #rszd._layers[l.name] = img
        self.isModified = True
        return rszd

    def view(self):
        return self.Zoom_coeff, self.xOffset, self.yOffset

    def setView(self, zoom=1.0, xOffset=0.0, yOffset=0.0):
        """
        Sets viewing conditions: zoom, offset
        @param zoom: zoom coefficient
        @type zoom: float
        @param xOffset: x-offset
        @type xOffset: int
        @param yOffset: y-offset
        @type yOffset: int
        @return: 
        """
        self.Zoom_coeff, self.xOffset, self.yOffset = zoom, xOffset, yOffset

    def fit_window(self, win):
        """
        reset Zoom_coeff and offset
        @param win: 
        @type win:
        """
        self.Zoom_coeff = 1.0
        self.xOffset, self.yOffset = 0.0, 0.0

class QLayer(vImage):
    """
    Base class for image layers
    """
    @classmethod
    def fromImage(cls, mImg, parentImage=None):
        """
        Returns a QLayer object initialized with mImg.
        @param mImg:
        @type mImg: QImage
        @param parentImage:
        @type parentImage: mImage
        @return:
        @rtype: Qlayer
        """
        layer = QLayer(QImg=mImg, parentImage=parentImage)
        layer.parentImage = parentImage
        return layer

    def __init__(self, *args, **kwargs):
        ############################################################
        # Signals
        # Making QLayer inherit from QObject leads to
        # a buggy behavior of hasattr and getattr.
        # So, we don't add signals as first level class attributes.
        # Instead, we use instances of ad hoc signal containers (cf. utils.py)

        self.visibilityChanged = baseSignal_bool()
        self.colorPicked = baseSignal_Int2()

        ###########################################################
        # self.parentImage = kwargs.get('parentImage', None)  #TODO added 11/09/18 validate removed 15/10/18 as duplicated
        # when a geometric transformation is applied to the whole image
        # each layer must be replaced with a transformed layer, recorded in tLayer
        # and tLayer.parentLayer keeps a reference to the original layer.
        self.tLayer = self
        self.parentLayer = self
        self.modified = False
        self.name='noname'
        self.visible = True
        # add signal instance (layer visibility change,..)
        #self.signals = baseSignals()
        self.isClipping = False
        self.role = kwargs.pop('role', '')
        self.tool = None
        self.parentImage = kwargs.pop('parentImage', None)
        # layer opacity is used by QPainter operations.
        # Its value must be in the range 0.0...1.0
        self.opacity = 1.0
        self.compositionMode = QPainter.CompositionMode_SourceOver
        # The next two attributes are used by adjustment layers only.
        self.execute = lambda l=None, pool=None: l.updatePixmap() if l is not None else None
        self.options = {}
        # actionName is used by methods graphics***.writeToStream()
        self.actionName = 'actionNull'
        # associated form : view is set by bLUe.menuLayer()
        self.view = None
        # containers are initialized (only once) by
        # getCurrentMaskedImage. Their type is QLayer
        self.maskedImageContainer = None
        self.maskedThumbContainer = None
        # consecutive layers can be grouped.
        # A group is a list of QLayer objects
        self.group = []
        # layer offsets
        self.xOffset, self.yOffset = 0, 0
        self.Zoom_coeff = 1.0
        # clone dup layer shift and zoom  relative to current layer
        self.xAltOffset, self.yAltOffset = 0, 0
        self.AltZoom_coeff = 1.0
        super().__init__(*args, **kwargs)
        self.updatePixmap() # TODO added 13/10/18 validate

    def getGraphicsForm(self):
        """
        Returns the graphics form associated to the layer
        @return:
        @rtype: QWidget
        """
        if self.view is not None:  #TODO added 2/10/18 validate
            return self.view.widget()
        return None

    def isActiveLayer(self):
        if self.parentImage.getActiveLayer() is self:
            return True
        return False

    def getMmcSpline(self):
        """
        Returns the spline used for multimode contrast
        correction if it is initialized and None otherwise.
        @return:
        @rtype: activeSpline
        """
        # get layer graphic form
        grf = self.getGraphicsForm()
        # manual curve form
        if grf.contrastForm is not None:
            return grf.contrastForm.scene().cubicItem
        return None

    def addTool(self, tool):
        """
        Adds tool to layer
        @param tool:
        @type tool: rotatingTool
        """
        self.tool = tool
        tool.modified = False
        tool.layer = self
        try:
            tool.layer.visibilityChanged.sig.disconnect()  # TODO signals removed 30/09/18 validate
        except:
            pass
        tool.layer.visibilityChanged.sig.connect(tool.setVisible) # TODO signals removed 30/09/18 validate
        tool.img = self.parentImage
        w, h = tool.img.width(), tool.img.height()
        for role, pos in zip(['topLeft', 'topRight', 'bottomRight', 'bottomLeft'],
                             [QPoint(0, 0), QPoint(w, 0), QPoint(w, h), QPoint(0, h)]):
            tool.btnDict[role].posRelImg = pos
            tool.btnDict[role].posRelImg_ori = pos
            tool.btnDict[role].posRelImg_frozen = pos
        tool.moveRotatingTool()

    def setVisible(self, value):
        """
        Sets self.visible to value and emit visibilityChanged.sig
        @param value:
        @type value: bool
        """
        self.visible = value
        self.visibilityChanged.sig.emit(value)  # TODO signals removed 30/09/18 validate

    def bTransformed(self, transformation, parentImage):
        """
        Applies transformation to a copy of layer and returns the copy.
        @param transformation:
        @type transformation: QTransform
        @param parentImage:
        @type parentImage: vImage
        @return: transformed layer
        @rtype: QLayer
        """
        # init a new layer from transformed image :
        # all static attributes (caches...) are reset to default, but thumb
        tLayer = QLayer.fromImage(self.transformed(transformation), parentImage=parentImage)
        # copy  dynamic attributes from old layer
        for a in self.__dict__.keys():
            if a not in tLayer.__dict__.keys():
                tLayer.__dict__[a] = self.__dict__[a]
        tLayer.name = self.name
        tLayer.actionName = self.actionName
        tLayer.view = self.view
        tLayer.visible = self.visible # TODO added 25/09/18 validate
        # cut link from old layer to graphic form
        # self.view = None                        # TODO 04/12/17 validate
        tLayer.execute = self.execute
        tLayer.mask = self.mask.transformed(transformation)
        tLayer.maskIsEnabled, tLayer.maskIsSelected = self.maskIsEnabled, self.maskIsSelected
        return tLayer

    def initThumb(self):
        """
        Override vImage.initThumb, to set the parentImage attribute
        """
        super().initThumb()
        self.thumb.parentImage = self.parentImage

    def initHald(self):
        """
        Build a hald image (as a QImage) from identity 3D LUT.
        """
        if not self.cachesEnabled:
            return
        s = int((LUT3DIdentity.size) ** (3.0 / 2.0)) + 1
        buf0 = LUT3DIdentity.toHaldArray(s, s).haldBuffer
        #self.hald = QLayer(QImg=QImage(QSize(190,190), QImage.Format_ARGB32))
        self.hald = QImage(QSize(s, s), QImage.Format_ARGB32)
        buf1 = QImageBuffer(self.hald)
        buf1[:, :, :3] = buf0
        buf1[:,:,3] = 255
        self.hald.parentImage = self.parentImage

    def getHald(self):
        if not self.cachesEnabled:
            s = int((LUT3DIdentity.size) ** (3.0 / 2.0)) + 1
            buf0 = LUT3DIdentity.toHaldArray(s, s).haldBuffer
            # self.hald = QLayer(QImg=QImage(QSize(190,190), QImage.Format_ARGB32))
            hald = QImage(QSize(s, s), QImage.Format_ARGB32)
            buf1 = QImageBuffer(hald)
            buf1[:, :, :3] = buf0
            buf1[:, :, 3] = 255
            hald.parentImage = self.parentImage
            return hald
        if self.hald is None:
            self.initHald()
        return self.hald

    def getCurrentImage(self):
        """
        Returns current (full, preview or hald) image, according to
        the value of the flags useThumb and useHald. The thumbnail and hald
        are computed if they are not initialized.
        Otherwise, they are not updated unless self.thumb is
        None or purgeThumb is True.
        Overrides vImage method
        @return: current image
        @rtype: QLayer
        """
        if self.parentImage.useHald:
            return self.getHald()
        if self.parentImage.useThumb:
            return self.getThumb()
        else:
            return self

    def inputImg(self):
        """
        Updates and returns maskedImageContainer and maskedThumbContainer
        @return:
        @rtype: Qlayer
        """
        # TODO 29/05/18 optimize to avoid useless updates of containers (add a valid flag to each container and invalidate it in cacheInvalidate())
        return self.parentImage.layersStack[self.getLowerVisibleStackIndex()].getCurrentMaskedImage()

    def full2CurrentXY(self, x, y):
        """
        Maps x,y coordinates of pixel in the full image to
        coordinates in current image.
        @param x:
        @type x: int or float
        @param y:
        @type y: int or float
        @return:
        @rtype: 2uple of int
        """
        if self.parentImage.useThumb:
            currentImg = self.getThumb()
            x = (x * currentImg.width()) / self.width()
            y = (y * currentImg.height()) / self.height()
        return int(x), int(y)

    def getCurrentMaskedImage(self):
        """
        Reduces the layer stack up to self (included),
        taking into account the masks. if self.isClipping is True
        self.mask applies to all lower layers and to self only otherwise.
        The method uses the non color managed rPixmaps to build the masked image.
        For convenience, mainly to be able to use its color space buffers,
        the built image is of type QLayer. It is drawn on a container image,
        created only once.
        @return: masked image
        @rtype: QLayer
        """
        # init containers if needed
        if self.parentImage.useHald:
            return self.getHald()
        if self.maskedThumbContainer is None:
            self.maskedThumbContainer = QLayer.fromImage(self.getThumb(), parentImage=self.parentImage)
        if self.maskedImageContainer is None:
            self.maskedImageContainer = QLayer.fromImage(self, parentImage=self.parentImage)
        if self.parentImage.useThumb:
            img = self.maskedThumbContainer
        else:
            img = self.maskedImageContainer
        # no thumbnails for containers
        img.getThumb = lambda: img
        # draw lower stack
        qp = QPainter(img)
        top = self.parentImage.getStackIndex(self)
        bottom = 0
        for i, layer in enumerate(self.parentImage.layersStack[bottom:top+1]):
            if layer.visible:
                if i == 0:
                    qp.setCompositionMode(QPainter.CompositionMode_Source)
                else:
                    qp.setOpacity(layer.opacity)
                    qp.setCompositionMode(layer.compositionMode)
                if layer.rPixmap is not None:
                    qp.drawPixmap(QRect(0,0,img.width(), img.height()), layer.rPixmap)
                else:
                    qp.drawImage(QRect(0,0,img.width(), img.height()), layer.getCurrentImage())
                # clipping
                if layer.isClipping and layer.maskIsEnabled: #TODO modified 23/06/18
                    # draw mask as opacity mask
                    # mode DestinationIn (set dest opacity to source opacity)
                    qp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                    omask = vImage.color2OpacityMask(layer.mask)
                    qp.drawImage(QRect(0, 0, img.width(), img.height()), omask)
        qp.end()
        """
        if self.isClipping and self.maskIsEnabled: #TODO modified 23/06/18
            mask = self.mask.scaled(img.width(), img.height())  # vImage.color2OpacityMask(self.mask)
            img = vImage.visualizeMask(img, mask, color=False, clipping=False, copy=False)
        """
        # buf = QImageBuffer(img) #TODO 24/09/18 removed validate
        return img

    def applyToStack(self):
        """
        Applies transformation and propagates changes to upper layers.
        """
        # recursive function
        def applyToStack_(layer, pool=None):
            # apply Transformation (call vImage.apply*LUT...)
            if layer.visible:
                start = time()
                layer.execute(l=layer)#, pool=pool) use default pool
                layer.cacheInvalidate()
                print("%s %.2f" %(layer.name, time()-start))
            stack = layer.parentImage.layersStack
            ind = layer.getStackIndex() + 1
            # get next visible upper layer
            while ind < len(stack):
                if stack[ind].visible:
                    break
                ind += 1
            if ind < len(stack):
                layer1 = stack[ind]
                applyToStack_(layer1, pool=pool)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            """
            if (not self.parentImage.useThumb or self.parentImage.useHald):
                pool = None
                # pool = multiprocessing.Pool(MULTIPROC_POOLSIZE)  # TODO time opt : pool is always created and used only by apply3DLUT; time 0.3s
            else:
                pool = None
            """
            applyToStack_(self, pool=None)
            """
            if pool is not None:
                pool.close()
            pool = None
            """
            # update the presentation layer
            self.parentImage.prLayer.execute(l=None, pool=None)
        finally:
            self.parentImage.setModified(True)
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()
    """
    def applyToStackIter(self):
        #iterative version of applyToStack
        stack = self.parentImage.layersStack
        ind = self.getStackIndex() + 1
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            self.execute()
            for layer in stack[ind:]:
                if layer.visible:
                    layer.cacheInvalidate()
                    # for hald friendly layer compute output hald, otherwise compute output image
                    layer.execute()
        finally:
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()
    """
    def isAdjustLayer(self):
        return self.view is not None #hasattr(self, 'view')

    def isSegmentLayer(self):
        return 'SEGMENT' in self.role

    def isCloningLayer(self):
        return 'CLONING' in self.role

    def isGeomLayer(self):
        return 'GEOM' in self.role

    def is3DLUTLayer(self):
        return '3DLUT' in self.role

    def isRawLayer(self):
        return 'RAW' in self.role

    def updatePixmap(self, maskOnly=False):
        """
        Updates the cached rPixmap.
        if maskIsEnabled is False, the mask is not shown.
        If maskIsEnabled is True, then
            - if maskIsSelected is True, the mask is drawn over
              the layer as a color mask.
            - if maskIsSelected is False, the mask is drawn as an
              opacity mask, setting image opacity to that of mask
              (mode DestinationIn). Mask color is no used.
        @param maskOnly: not used yet
        @type maskOnly: boolean
        """
        rImg = self.getCurrentImage()
        # apply layer transformation. Missing pixels are set to QColor(0,0,0,0)
        if self.xOffset != 0 or self.yOffset != 0:
            x,y = self.full2CurrentXY(self.xOffset, self.yOffset)
            rImg = rImg.copy(QRect(-x, -y, rImg.width()*self.Zoom_coeff, rImg.height()*self.Zoom_coeff))
        if self.maskIsEnabled:
            rImg = vImage.visualizeMask(rImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
        self.rPixmap = QPixmap.fromImage(rImg)
        self.setModified(True)

    def getStackIndex(self):
        """
        Returns layer index in the stack, len(stack) - 1 if
        the layer is not in the stack.
        @return:
        @rtype: int
        """
        for i, l in enumerate(self.parentImage.layersStack):
            if l is self:
                break
        return i

    def updateTableView(self, table):
        """
        refreshes the corresponding row in table
        @param table:
        @type table: QTableView
        """
        ind = self.getStackIndex()
        table.updateRow(len(self.parentImage.layersStack) - 1 - ind)

    def getTopVisibleStackIndex(self):
        """
        Returns the index of the top visible layer
        @return:
        @rtype:
        """
        stack = self.parentImage.layersStack
        lg = len(stack)
        for i in range(lg-1, -1, -1):
            if stack[i].visible:
                return i
        return -1
    def getLowerVisibleStackIndex(self):
        """
        Returns the index of the next lower visible layer,
        -1 if it does not exists
        @return:
        @rtype: int
        """
        ind = self.getStackIndex()
        stack = self.parentImage.layersStack
        for i in range(ind-1, -1, -1):
            if stack[i].visible:
                return i
        return -1

    def getUpperVisibleStackIndex(self):
        """
        Returns the index of the next upper visible layer,
        -1 if it does not exists
        @return:
        @rtype: int
        """
        ind = self.getStackIndex()
        stack = self.parentImage.layersStack
        lg = len(stack)
        for i in range(ind+1, lg, 1):
            if stack[i].visible:
                return i
        return -1

    def getLowerClippingStackIndex(self):
        """
         Returns the index of the next lower clipping layer,
        -1 if it does not exists

        @return:
        @rtype: int
        """
        ind = self.getStackIndex()
        for i in range(ind + 1, len(self.parentImage.layersStack), 1):
            if self.parentImage.layersStack[i].isClipping:
                return i
        return -1

    def linkMask2Lower(self):
        """
        share mask with next lower layer
        @return:
        @rtype:
        """
        ind = self.getStackIndex()
        if ind == 0:
            return
        lower = self.parentImage.layersStack[ind-1]
        # don't link two groups
        if self.group and lower.group:
            return
        if not self.group and not lower.group:
            self.group = [self, lower]
            lower.group = self.group
        elif not lower.group :
            if not any(o is lower for o in self.group):
                self.group.append(lower)
            lower.group  = self.group
        elif not self.group:
            if not any(item is self for item in lower.group):
                lower.group.append(self)
            self.group = lower.group
        self.mask = lower.mask

    def unlinkMask(self):
        self.mask =self.mask.copy()
        # remove self from group
        for i,item in enumerate(self.group):
            if item is self:
                self.group.pop(i)
                # don't keep  group with length 1
                if len(self.group) == 1:
                    self.group.pop(0)
                break
        self.group = []

    def merge_with_layer_immediately_below(self):
        """
        Merges a layer with the next lower visible layer. Does nothing
        if mode is preview or the target layer is an adjustment layer.
        """
        if not hasattr(self, 'inputImg'):
            return
        ind = self.getLowerVisibleStackIndex()
        if ind < 0:
            # no visible layer found
            return
        target = self.parentImage.layersStack[ind]
        if hasattr(target, 'inputImg') or self.parentImage.useThumb:
            info = "Uncheck Preview first" if self.parentImage.useThumb else "Target layer must be background or image"
            dlgWarn("Cannot Merge layers", info=info)
            return
        #update stack
        self.parentImage.layersStack[0].applyToStack()
        # merge
        #target.setImage(self)
        qp = QPainter(target)
        qp.setCompositionMode(self.compositionMode)
        qp.setOpacity(self.opacity)
        qp.drawImage(QRect(0,0,self.width(), self.height()), self)
        target.updatePixmap()
        self.parentImage.layerView.clear(delete=False)
        currentIndex = self.getStackIndex()
        self.parentImage.activeLayerIndex = ind
        self.parentImage.layersStack.pop(currentIndex)
        self.parentImage.layerView.setLayers(self.parentImage)

    def reset(self):
        """
        reset layer to inputImg
        @return:
        """
        self.setImage(self.inputImg())

    def setOpacity(self, value):
        """
        set the opacity attribute to value/100.0
        @param value:
        """
        self.opacity = value /100.0
        return

    def readFromStream(self, dataStream):

        if hasattr(self, 'view'):
            self.view.widget().readFromStream(dataStream)
        return dataStream

class QPresentationLayer(QLayer):
    """
    A presentation layer is used for color management. It is an
    adjustment layer that does nothing. It does not belong to the layer stack :
    conceptually, it is "above" the stack, so it holds the composition of
    all stacked layers. It is the sole color managed layer, via its qPixmap
    attribute.
    """

    def __init__(self, *args, **kwargs):
        self.qPixmap = None
        self.cmImage = None
        super().__init__(*args, **kwargs)

    def inputImg(self):
        return self.parentImage.layersStack[self.getTopVisibleStackIndex()].getCurrentMaskedImage()

    def updatePixmap(self, maskOnly = False):
        """
        Updates the caches qPixmap, rPixmap and cmImage.
        The input image is that returned by getCurrentImage(), thus
        the caches are synchronized using the current image
        mode (full or preview).
        If maskOnly is True, cmImage is not updated.
        if maskIsEnabled is False, the mask is not shown.
        If maskIsEnabled is True, then
            - if maskIsSelected is True, the mask is drawn over
              the layer as a color mask.
            - if maskIsSelected is False, the mask is drawn as an
              opacity mask, setting image opacity to that of mask
              (mode DestinationIn). Mask color is no used.
        @param maskOnly: default False
        @type maskOnly: boolean
        """
        currentImage = self.getCurrentImage()
        # apply color management to presentation layer
        if icc.COLOR_MANAGE and self.parentImage is not None and getattr(self, 'role', None) == 'presentation':
            # CAUTION : reset alpha channel
            img = convertQImage(currentImage, transformation=self.parentImage.colorTransformation)  # time 0.66 s for 15 Mpx.
            # restore alpha channel
            # img = img.convertToFormat(currentImage.format()) # TODO 15/10/18 dome by convertQImage()
            buf0 = QImageBuffer(img)
            buf1 = QImageBuffer(currentImage)
            buf0[:,:,3] = buf1[:,:,3]
        else:
            img = currentImage
        qImg = img
        rImg = currentImage
        if self.maskIsEnabled:
            #qImg = vImage.visualizeMask(qImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
            rImg = vImage.visualizeMask(rImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
        self.qPixmap = QPixmap.fromImage(qImg)
        self.rPixmap = QPixmap.fromImage(rImg)
        self.setModified(True)

    def update(self):
        self.applyNone()
        # self.updatePixmap() done by applyNone()

class QLayerImage(QLayer):
    """
    QLayer holding a source image
    """
    @classmethod
    def fromImage(cls, mImg, parentImage=None, sourceImg=None):
        layer = QLayerImage(QImg=mImg, parentImage=parentImage)
        layer.parentImage = parentImage
        layer.sourceImg = sourceImg
        return layer

    def __init__(self, *args, **kwargs):
        self.sourceImg = QImage()
        super().__init__(*args, **kwargs)

    def bTransformed(self, transformation, parentImage):
        """
        Applies transformation to a copy of layer and returns the copy.
        @param transformation:
        @type transformation: QTransform
        @param parentImage:
        @type parentImage: vImage
        @return: transformed layer
        @rtype: QLayerImage
        """
        # TODO modified 26/09/18 validate
        tLayer = super().bTransformed(self, transformation, parentImage)
        """ 
        tLayer = QLayerImage.fromImage(self.transformed(transformation), parentImage=parentImage, sourceImg=self.sourceImg.transformed(transformation))
        # add dynamic attributes
        for a in self.__dict__.keys():
            av = tLayer.__dict__.get(a, None)
            if av is None or av == '':
                tLayer.__dict__[a] = self.__dict__[a]
        # link back grWindow to tLayer
        if tLayer.view is not None:
            tLayer.view.widget().layer = tLayer
        if tLayer.tool is not None:
            tLayer.tool.layer = tLayer
            tLayer.tool.img = tLayer.parentImage
        tLayer.execute = self.execute
        tLayer.mask = self.mask.transformed(transformation)
        tLayer.maskIsEnabled, tLayer.maskIsSelected = self.maskIsEnabled, self.maskIsSelected
        # keep original layer
        tLayer.parentLayer = self.parentLayer
        # record new transformed layer
        self.parentLayer.tLayer = tLayer
        """
        if tLayer.tool is not None:
            tLayer.tool.layer = tLayer
            tLayer.tool.img = tLayer.parentImage
        return tLayer

    def inputImg(self):
        """
        Overrides QLayer.inputImg()
        @return:
        @rtype: QImage
        """
        return self.sourceImg.scaled(self.getCurrentImage().size())






