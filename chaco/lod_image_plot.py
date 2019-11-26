from contextlib import contextmanager

import numpy as np

from enable.base import intersect_bounds
from kiva.quartz.ABCGI import InterpolationQuality
from traits.api import (
    Bool, Event, Instance, Int, List, Property,
    cached_property, on_trait_change
)

try:
    from encore.concurrent.futures.serializer import Serializer
    from encore.concurrent.futures.enhanced_thread_pool_executor import \
        EnhancedThreadPoolExecutor
except ImportError:
    import sys
    sys.exit('encore must be installed to use LODImagePlot')

from .lod_image_source import LODImageSource
from .image_plot import ImagePlot


KIVA_INTERP_QUALITY = {"nearest": InterpolationQuality.none,
                       "bilinear": InterpolationQuality.low,
                       "bicubic": InterpolationQuality.high}


class LODImagePlot(ImagePlot):
    """Image renderer using data sources that don't directly expose their data
    and has pre-calculated LOD images.
    Cached image is invalidated upon new drawing bounds and computed with
    decreasing level of detail (LOD) number (increasing resolution).
    """

    #: The data source to use as value points.
    value = Instance(LODImageSource)

    #: Draw position and draw bounds
    draw_bounds = List

    #: Draw bounds
    draw_size = List

    #: Current rendering LOD
    lod = Int

    #: Minimum LOD necessary so that loaded image's resolution is high enough
    #: for the drawing area
    necessary_lod = Property(Int, depends_on="draw_size[]")

    #: Maximum LOD allowed
    maximum_lod = Int(9)

    #: Event that a new image cache is computed
    new_cache_ready = Event

    #: The executor to compute cache images
    executor = Instance(EnhancedThreadPoolExecutor)

    #: The serializer to serialize cache computation jobs
    _serializer = Instance(Serializer)

    #------------------------------------------------------------------------
    # Defaults
    #------------------------------------------------------------------------

    def __serializer_default(self):
        return Serializer(name='Cache Computation Serializer',
                          executor=self.executor)

    #------------------------------------------------------------------------
    # Properties
    #------------------------------------------------------------------------

    @cached_property
    def _get_necessary_lod(self):
        """ Calculates the largest LOD that the corresponding LOD image has
        more pixes in both x and y than the screen area.
        """
        image_rect = self._calc_virtual_screen_bbox()
        index_bounds, screen_rect = self._calc_zoom_coords(image_rect)
        screen_size = screen_rect[2:]
        lod = self.maximum_lod
        col_min, col_max, row_min, row_max = \
            self.value.lod_bounds_from_nominal_bounds(index_bounds, lod)
        while ((col_max - col_min) < screen_size[0]) or \
              ((row_max - row_min) < screen_size[1]):
            lod -= 1
            col_min, col_max, row_min, row_max = \
                self.value.lod_bounds_from_nominal_bounds(index_bounds, lod)
        return lod

    #------------------------------------------------------------------------
    # Traits handlers
    #------------------------------------------------------------------------

    @on_trait_change("draw_bounds[]")
    def handle_draw_bounds_change(self):
        self.lod = self.maximum_lod
        # Skip jobs in the serializer which all have outdated draw bounds
        self._serializer._pending_operations.clear()
        self.invalidate_draw()

    @on_trait_change("lod", dispatch="ui")
    def handle_lod_level_change(self):
        self.invalidate_draw()

    @on_trait_change("new_cache_ready", dispatch="ui")
    def handle_new_cached(self):
        """ Adds request redraw into the UI event loop to render the newly
        cached image.
        """
        self.request_redraw()

    #------------------------------------------------------------------------
    # Base2DPlot interface
    #------------------------------------------------------------------------

    def invalidate_draw(self):
        self._image_cache_valid = False
        super(LODImagePlot, self).invalidate_draw()

    def _render(self, gc):
        """ Submits a job to compute cached image if current cache is invalid
        and renders the current cached image regardless of its validity.

        Cache image computation can be time consuming, thus is submitted to the
        serializer to avoid blocking. Once new cache is computed, new request
        to redraw will be added to the GUI event loop by traits notifications.
        """
        if not self._image_cache_valid:
            self._serializer.submit(self._compute_cached_image)

        if self._cached_image is not None:
            self._render_image_in_rect(gc, self._cached_image,
                                       self._cached_dest_rect)

    def _draw_image(self, gc, view_bounds, mode="normal"):
        # Intercept the view bounds info here to update draw bounds
        new_bounds = list(
            intersect_bounds(self.position + self.bounds, view_bounds)
        )
        self.draw_bounds = new_bounds
        self.draw_size = new_bounds[2:]
        super(LODImagePlot, self)._draw_image(gc)

    #------------------------------------------------------------------------
    # ImagePlot interface
    #------------------------------------------------------------------------

    def _compute_cached_image(self, mapper=None):
        """ Computes the correct sub-image coordinates and renders an image
        into self._cached_image.

        Parameters
        ----------
        mapper : function
            Allows subclasses to transform the displayed values for the visible
            region. This may be used to adapt grayscale images to RGB(A)
            images.
        """
        virtual_rect = self._calc_virtual_screen_bbox()
        index_bounds, screen_rect = self._calc_zoom_coords(virtual_rect)

        data = self._get_data_slice(index_bounds, mapper)

        # Update cached image and rectangle.
        self._cached_image = self._kiva_array_from_numpy_array(data)
        self._cached_dest_rect = screen_rect
        self._image_cache_valid = True
        # Update the event so its handler will add redraw request to
        # the GUI event loop
        self.new_cache_ready = True

        # Next time compute image cache with higher resolution
        if self.lod > self.necessary_lod:
            self.lod -= 1

    #------------------------------------------------------------------------
    # Private methods
    #------------------------------------------------------------------------

    def _render_image_in_rect(self, gc, kiva_array, screen_rect):
        """ Draw the kiva array to a rectangle in screen-space. """

        if kiva_array is None:
            return

        scale_x = -1 if self.x_axis_is_flipped else 1
        scale_y = 1 if self.y_axis_is_flipped else -1

        x, y, w, h = screen_rect
        x_center = x + w / 2
        y_center = y + h / 2
        with gc:
            gc.clip_to_rect(self.x, self.y, self.width, self.height)
            gc.set_alpha(self.alpha)

            # Translate origin to the center of the graphics context.
            if self.orientation == "h":
                gc.translate_ctm(x_center, y_center)
            else:
                gc.translate_ctm(y_center, x_center)

            # Flip axes to move origin to the correct position.
            gc.scale_ctm(scale_x, scale_y)

            if self.orientation == "v":
                self._transpose_about_origin(gc)

            # Translate the origin back to its original position.
            gc.translate_ctm(-x_center, -y_center)

            with self._temporary_interp_setting(gc, kiva_array):
                gc.draw_image(kiva_array, screen_rect)

    @contextmanager
    def _temporary_interp_setting(self, gc, kiva_array):
        if hasattr(gc, "set_interpolation_quality"):
            # Quartz uses interpolation setting on the destination GC.
            interp_quality = KIVA_INTERP_QUALITY[self.interpolation]
            gc.set_interpolation_quality(interp_quality)
            yield
        elif hasattr(gc, "set_image_interpolation"):
            # Agg backend uses the interpolation setting of the *source*
            # image to determine the type of interpolation to use when
            # drawing. Temporarily change image's interpolation value.
            old_interp = kiva_array.get_image_interpolation()
            set_interp = kiva_array.set_image_interpolation
            try:
                set_interp(self.interpolation)
                yield
            finally:
                set_interp(old_interp)

    def _get_data_slice(self, index_bounds, mapper):
        """ Gets data by nominal index bounds

        Parameters
        ----------
        index_bounds : 4-tuple
            Column and row indices (col_min, col_max, row_min, row_max)
            representing desired slices into the data. If None, return sensible
            default.
        mapper : function
            Allows subclasses to transform the displayed values for the visible
            region. This may be used to adapt grayscale images to RGB(A)
            images.

        Returns
        -------

        """
        data = self.value.get_data_bounded(
            index_bounds, self.lod)

        if mapper is not None and data.size > 0:
            data = mapper(data)

        return data

    def _calc_zoom_coords(self, image_rect):
        """ Calculates the coordinates of the current zoomed sub-image.

        Because of floating point limitations, it is not advisable to request a
        extreme level of zoom, e.g., idx or idy > 10^10.

        Parameters
        ----------
        image_rect : 4-tuple
            (x, y, width, height) rectangle describing the pixels bounds of the
            full, **rendered** image. This will be larger than the canvas when
            zoomed in since the full image may not fit on the canvas.

        Returns
        -------
        index_bounds : 4-tuple
            The column and row indices (col_min, col_max, row_min, row_max) of
            the sub-image to be extracted and drawn into `screen_rect`.
        screen_rect : 4-tuple
            (x, y, width, height) rectangle describing the pixels bounds where
            the image will be rendered in the plot.
        """
        sub_x, sub_y, sub_width, sub_height = self.draw_bounds
        if 0 in (sub_width, sub_height) or 0 in self.bounds:
            return None, None

        image_x, image_y, image_width, image_height = image_rect
        array_width = self.value.get_width() - 1
        array_height = self.value.get_height() - 1

        col_min = int((sub_x - image_x) / image_width * array_width)
        col_max = int((sub_x - image_x + sub_width) / image_width
                      * array_width)
        row_min = int((sub_y - image_y) / image_height * array_height)
        row_max = int((sub_y - image_y + sub_height) / image_height *
                      array_height)

        # Clip index bounds to the array bounds.
        col_min = max(col_min, 0)
        col_max = min(col_max, array_width)
        row_min = max(row_min, 0)
        row_max = min(row_max, array_height)

        # Flip indexes **after** calculating screen coordinates.
        # The screen coordinates will get flipped in the renderer.
        if self.y_axis_is_flipped:
            row_min = array_height - row_min
            row_max = array_height - row_max
            row_min, row_max = row_max, row_min
        if self.x_axis_is_flipped:
            col_min = array_width - col_min
            col_max = array_width - col_max
            col_min, col_max = col_max, col_min

        index_bounds = (col_min, col_max, row_min, row_max)
        screen_rect = self.draw_bounds
        return index_bounds, screen_rect



