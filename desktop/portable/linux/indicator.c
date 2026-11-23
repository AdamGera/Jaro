#include <gtk/gtk.h>
#include <gdk/x11/gdkx.h>
#include <X11/extensions/shape.h>
#include <gtk4-layer-shell.h>
#include <string.h>
#include <unistd.h>
#include "../dictation_meter.h"

static Meter meter;
static gint64 started;
static GMainLoop *loop;

static void capsule(cairo_t *cr, double x, double y, double w, double h) {
    double radius = fmin(w, h) / 2;
    cairo_new_sub_path(cr);
    cairo_arc(cr, x + w - radius, y + radius, radius, -G_PI_2, 0);
    cairo_arc(cr, x + w - radius, y + h - radius, radius, 0, G_PI_2);
    cairo_arc(cr, x + radius, y + h - radius, radius, G_PI_2, G_PI);
    cairo_arc(cr, x + radius, y + radius, radius, G_PI, G_PI * 1.5);
    cairo_close_path(cr);
}

static void paint(GtkDrawingArea *area, cairo_t *cr, int w, int h, gpointer data) {
    (void)area; (void)data;
    capsule(cr, .5, .5, w - 1, h - 1);
    cairo_set_source_rgb(cr, .090, .094, .141);
    cairo_fill_preserve(cr);
    cairo_set_source_rgb(cr, .267, .267, .345);
    cairo_set_line_width(cr, 1);
    cairo_stroke(cr);
    if (meter.processing) cairo_set_source_rgb(cr, .565, .780, .745);
    else cairo_set_source_rgb(cr, .8, .765, 1);
    double elapsed = (g_get_monotonic_time() - started) / 1000000.0;
    for (int i = 0; i < METER_BARS; i++) {
        double height = meter_height(i, elapsed, meter.level, meter.processing);
        double x = (w - METER_BAR_WIDTH) / 2.0 + (i - 2) * METER_GAP;
        capsule(cr, x, (h - height) / 2.0, METER_BAR_WIDTH, height);
        cairo_fill(cr);
    }
}

static gboolean frame(GtkWidget *widget, GdkFrameClock *clock, gpointer data) {
    (void)clock; (void)data;
    gtk_widget_queue_draw(widget);
    return G_SOURCE_CONTINUE;
}

static gboolean receive(GIOChannel *pipe, GIOCondition event, gpointer data) {
    (void)pipe; (void)data;
    if (event & G_IO_IN) {
        char buffer[256];
        ssize_t bytes = read(STDIN_FILENO, buffer, sizeof(buffer));
        if (bytes > 0) {
            meter_read(&meter, buffer, (unsigned int)bytes);
            return G_SOURCE_CONTINUE;
        }
    }
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

static gboolean place_x11(GdkSurface *surface, gboolean bottom) {
    GdkDisplay *display = gdk_surface_get_display(surface);
    // GTK has no portable override-redirect window API.
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    Display *xdisplay = gdk_x11_display_get_xdisplay(display);
    Window window = gdk_x11_surface_get_xid(surface);
    G_GNUC_END_IGNORE_DEPRECATIONS
    int major, minor;
    if (!XShapeQueryVersion(xdisplay, &major, &minor) || (major == 1 && minor < 1)) return FALSE;
    Window root = DefaultRootWindow(xdisplay);
    Window pointer_root, pointer_child;
    int x = 0, y = 0, unused_x, unused_y;
    unsigned int modifiers;
    XQueryPointer(xdisplay, root, &pointer_root, &pointer_child, &x, &y, &unused_x, &unused_y, &modifiers);
    int scale = gdk_surface_get_scale_factor(surface);
    GdkRectangle box = {0, 0, DisplayWidth(xdisplay, DefaultScreen(xdisplay)) / scale,
                              DisplayHeight(xdisplay, DefaultScreen(xdisplay)) / scale};
    GListModel *monitors = gdk_display_get_monitors(display);
    for (guint i = 0; i < g_list_model_get_n_items(monitors); i++) {
        GdkMonitor *monitor = g_list_model_get_item(monitors, i);
        GdkRectangle geometry;
        gdk_monitor_get_geometry(monitor, &geometry);
        g_object_unref(monitor);
        if (x / scale >= geometry.x && x / scale < geometry.x + geometry.width &&
            y / scale >= geometry.y && y / scale < geometry.y + geometry.height) {
            box = geometry;
            break;
        }
    }
    int width = METER_WIDTH * scale, height = METER_HEIGHT * scale;
    int px = (box.x + (box.width - METER_WIDTH) / 2) * scale;
    int py = (bottom ? box.y + box.height - METER_HEIGHT - METER_MARGIN : box.y + METER_MARGIN) * scale;
    XSetWindowAttributes attributes = {.override_redirect = True};
    XChangeWindowAttributes(xdisplay, window, CWOverrideRedirect, &attributes);
    XWMHints hints = {.flags = InputHint, .input = False};
    XSetWMHints(xdisplay, window, &hints);
    XMoveWindow(xdisplay, window, px, py);
    XRectangle *outline = g_new(XRectangle, height);
    double radius = height / 2.0;
    for (int row = 0; row < height; row++) {
        double distance = row + .5 - radius;
        int inset = (int)(radius - sqrt(radius * radius - distance * distance));
        outline[row] = (XRectangle){inset, row, width - 2 * inset, 1};
    }
    XShapeCombineRectangles(xdisplay, window, ShapeBounding, 0, 0, outline, height, ShapeSet, YSorted);
    g_free(outline);
    XFlush(xdisplay);
    return TRUE;
}

int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "top") && strcmp(argv[1], "bottom"))) return 2;
    if (!gtk_init_check()) return 3;
    gboolean x11 = GDK_IS_X11_DISPLAY(gdk_display_get_default());
    if (!x11 && !gtk_layer_is_supported()) {
        if (g_getenv("DISPLAY") && g_setenv("GDK_BACKEND", "x11", TRUE)) execv(argv[0], argv);
        return 3;
    }
    gboolean bottom = strcmp(argv[1], "bottom") == 0;
    GtkWindow *window = GTK_WINDOW(gtk_window_new());
    gtk_window_set_title(window, "Jaro Dictation Indicator");
    gtk_window_set_decorated(window, FALSE);
    gtk_window_set_resizable(window, FALSE);
    gtk_window_set_default_size(window, METER_WIDTH, METER_HEIGHT);
    gtk_widget_set_focusable(GTK_WIDGET(window), FALSE);
    gtk_widget_add_css_class(GTK_WIDGET(window), "jaro-dictation-indicator");
    GtkCssProvider *style = gtk_css_provider_new();
    gtk_css_provider_load_from_string(style, ".jaro-dictation-indicator { background: transparent; box-shadow: none; }");
    gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(style),
                                               GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(style);
    if (!x11) {
        GtkLayerShellEdge edge = bottom ? GTK_LAYER_SHELL_EDGE_BOTTOM : GTK_LAYER_SHELL_EDGE_TOP;
        gtk_layer_init_for_window(window);
        gtk_layer_set_namespace(window, "jaro-dictation");
        gtk_layer_set_layer(window, GTK_LAYER_SHELL_LAYER_OVERLAY);
        gtk_layer_set_keyboard_mode(window, GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
        gtk_layer_set_anchor(window, edge, TRUE);
        gtk_layer_set_margin(window, edge, METER_MARGIN);
        gtk_layer_set_exclusive_zone(window, 0);
    }
    GtkWidget *area = gtk_drawing_area_new();
    gtk_window_set_child(window, area);
    gtk_drawing_area_set_draw_func(GTK_DRAWING_AREA(area), paint, NULL, NULL);
    gtk_widget_realize(GTK_WIDGET(window));
    GdkSurface *surface = gtk_native_get_surface(GTK_NATIVE(window));
    if (x11 && !place_x11(surface, bottom)) return 3;
    cairo_region_t *empty = cairo_region_create();
    gdk_surface_set_input_region(surface, empty);
    cairo_region_destroy(empty);
    GIOChannel *pipe = g_io_channel_unix_new(STDIN_FILENO);
    g_io_add_watch(pipe, G_IO_IN | G_IO_HUP | G_IO_ERR | G_IO_NVAL, receive, NULL);
    g_io_channel_unref(pipe);
    started = g_get_monotonic_time();
    gtk_widget_add_tick_callback(area, frame, NULL, NULL);
    gtk_widget_set_visible(GTK_WIDGET(window), TRUE);
    loop = g_main_loop_new(NULL, FALSE);
    g_main_loop_run(loop);
    gtk_window_destroy(window);
    g_main_loop_unref(loop);
    return 0;
}
