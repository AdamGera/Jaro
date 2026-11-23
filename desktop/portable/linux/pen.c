#include <gtk/gtk.h>
#include <gtk4-layer-shell.h>
#include <glib-unix.h>
#include <locale.h>
#include <math.h>
#include <stdio.h>
#include <unistd.h>

#define MAX_POINTS 1024
#define INK_HOLD_US 650000
#define INK_LIFETIME_US 1300000

typedef struct {
    double x, y;
    gint64 time;
} Point;

typedef struct {
    Point points[MAX_POINTS];
    guint count;
    gint64 released;
} Stroke;

typedef struct {
    GtkWidget *area;
    GMainLoop *loop;
    GPtrArray *strokes;
    Stroke *current;
    double left, top, right, bottom;
    double origin_x, origin_y;
    gboolean ready;
} Pen;

static GdkMonitor *find_monitor(GdkDisplay *display, const char *connector, const char *geometry) {
    GListModel *monitors = gdk_display_get_monitors(display);
    guint count = g_list_model_get_n_items(monitors);
    GdkRectangle requested;
    gboolean positioned = sscanf(geometry, "%d,%d,%d,%d", &requested.x, &requested.y,
                                 &requested.width, &requested.height) == 4;
    GdkMonitor *match = NULL;
    for (guint index = 0; index < count; index++) {
        GdkMonitor *monitor = g_list_model_get_item(monitors, index);
        GdkRectangle bounds;
        gdk_monitor_get_geometry(monitor, &bounds);
        const char *name = gdk_monitor_get_connector(monitor);
        gboolean same_connector = *connector && name && g_str_equal(name, connector);
        gboolean same_geometry = positioned && gdk_rectangle_equal(&bounds, &requested);
        if (positioned ? same_geometry : (same_connector || count == 1)) {
            if (match) {
                g_object_unref(match);
                g_object_unref(monitor);
                return NULL;
            }
            match = monitor;
        } else {
            g_object_unref(monitor);
        }
    }
    return match;
}

static void set_input_region(Pen *pen, GdkSurface *surface, int width, int height) {
    int left = floor(pen->left * width), top = floor(pen->top * height);
    cairo_rectangle_int_t bounds = {left, top, ceil(pen->right * width) - left, ceil(pen->bottom * height) - top};
    cairo_region_t *region = cairo_region_create_rectangle(&bounds);
    gdk_surface_set_input_region(surface, region);
    cairo_region_destroy(region);
}

static void paint(GtkDrawingArea *area, cairo_t *cr, int width, int height, gpointer data) {
    Pen *pen = data;
    double left = pen->left * width, top = pen->top * height;
    double draw_width = (pen->right - pen->left) * width, draw_height = (pen->bottom - pen->top) * height;
    double radius = MAX(1.5, MIN(draw_width, draw_height) * .0035);
    gint64 now = g_get_monotonic_time();
    cairo_rectangle(cr, left, top, draw_width, draw_height);
    cairo_clip(cr);
    cairo_set_line_width(cr, radius * 2);
    cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);
    cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND);
    for (guint index = 0; index < pen->strokes->len; index++) {
        Stroke *stroke = g_ptr_array_index(pen->strokes, index);
        double age = stroke->released ? now - stroke->released : 0;
        double opacity = CLAMP((INK_LIFETIME_US - age) / (INK_LIFETIME_US - INK_HOLD_US), 0, 1);
        cairo_set_source_rgba(cr, 1, .4, .22, opacity);
        double x = left + stroke->points[0].x * draw_width, y = top + stroke->points[0].y * draw_height;
        if (stroke->count == 1) {
            cairo_arc(cr, x, y, radius, 0, 2 * G_PI);
            cairo_fill(cr);
        } else {
            cairo_move_to(cr, x, y);
            for (guint i = 1; i < stroke->count; i++)
                cairo_line_to(cr, left + stroke->points[i].x * draw_width, top + stroke->points[i].y * draw_height);
            cairo_stroke(cr);
        }
    }
    (void)area;
}

static void resize(GtkDrawingArea *area, int width, int height, gpointer data) {
    Pen *pen = data;
    GtkNative *native = gtk_widget_get_native(GTK_WIDGET(area));
    GdkSurface *surface = gtk_native_get_surface(native);
    set_input_region(pen, surface, width, height);
    if (!pen->ready && width > 1 && height > 1) {
        pen->ready = TRUE;
        puts("READY");
        fflush(stdout);
    }
}

static gboolean animate(gpointer data) {
    Pen *pen = data;
    gint64 now = g_get_monotonic_time();
    gboolean redraw = FALSE;
    for (guint i = pen->strokes->len; i > 0; i--) {
        Stroke *stroke = g_ptr_array_index(pen->strokes, i - 1);
        if (stroke->released && now - stroke->released >= INK_HOLD_US)
            redraw = TRUE;
        if (stroke->released && now - stroke->released >= INK_LIFETIME_US)
            g_ptr_array_remove_index(pen->strokes, i - 1);
    }
    if (redraw)
        gtk_widget_queue_draw(pen->area);
    return G_SOURCE_CONTINUE;
}

static void add_point(Pen *pen, double x, double y) {
    Stroke *stroke = pen->current;
    int width = gtk_widget_get_width(pen->area), height = gtk_widget_get_height(pen->area);
    Point point = {
        CLAMP((x / width - pen->left) / (pen->right - pen->left), 0, 1),
        CLAMP((y / height - pen->top) / (pen->bottom - pen->top), 0, 1),
        g_get_monotonic_time()
    };
    if (stroke->count) {
        Point last = stroke->points[stroke->count - 1];
        if (fabs(last.x - point.x) < .0005 && fabs(last.y - point.y) < .0005)
            return;
    }
    if (stroke->count == MAX_POINTS) {
        // Keep the whole path for long, continuous strokes.
        for (guint i = 1; i < MAX_POINTS / 2; i++)
            stroke->points[i] = stroke->points[i * 2];
        stroke->count = MAX_POINTS / 2;
    }
    stroke->points[stroke->count++] = point;
    gtk_widget_queue_draw(pen->area);
}

static void release_stroke(Pen *pen) {
    Stroke *stroke = pen->current;
    if (!stroke) return;
    pen->current = NULL;
    stroke->released = g_get_monotonic_time();
    printf("{\"released\":%" G_GINT64_FORMAT ",\"points\":[", stroke->released);
    for (guint i = 0; i < stroke->count; i++) {
        Point point = stroke->points[i];
        printf("%s[%.5f,%.5f,%" G_GINT64_FORMAT "]", i ? "," : "", point.x, point.y, point.time);
    }
    puts("]}");
    fflush(stdout);
}

static void drag_begin(GtkGestureDrag *gesture, double x, double y, gpointer data) {
    Pen *pen = data;
    (void)gesture;
    if (!pen->ready) return;
    release_stroke(pen);
    pen->origin_x = x;
    pen->origin_y = y;
    pen->current = g_new0(Stroke, 1);
    g_ptr_array_add(pen->strokes, pen->current);
    add_point(pen, x, y);
}

static void drag_update(GtkGestureDrag *gesture, double x, double y, gpointer data) {
    Pen *pen = data;
    (void)gesture;
    if (pen->current)
        add_point(pen, pen->origin_x + x, pen->origin_y + y);
}

static void drag_end(GtkGestureDrag *gesture, double x, double y, gpointer data) {
    drag_update(gesture, x, y, data);
    release_stroke(data);
}

static void cancel_drag(GtkGesture *gesture, GdkEventSequence *sequence, gpointer data) {
    (void)gesture; (void)sequence;
    release_stroke(data);
}

static gboolean close_overlay(GtkWindow *window, gpointer data) {
    Pen *pen = data;
    (void)window;
    g_main_loop_quit(pen->loop);
    return TRUE;
}

static void output_changed(GListModel *monitors, guint position, guint removed, guint added, gpointer data) {
    (void)monitors; (void)position; (void)removed; (void)added;
    close_overlay(NULL, data);
}

static void monitor_changed(GObject *monitor, GParamSpec *property, gpointer data) {
    (void)monitor; (void)property;
    close_overlay(NULL, data);
}

static gboolean key_pressed(GtkEventControllerKey *key, guint value, guint code, GdkModifierType state, gpointer data) {
    (void)key; (void)code; (void)state;
    return value == GDK_KEY_Escape && close_overlay(NULL, data);
}

static void secondary(GtkGestureClick *click, int presses, double x, double y, gpointer data) {
    (void)click; (void)presses; (void)x; (void)y;
    close_overlay(NULL, data);
}

static gboolean parent_closed(int fd, GIOCondition status, gpointer data) {
    (void)fd; (void)status;
    close_overlay(NULL, data);
    return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv) {
    gboolean probe = argc == 4 && g_str_equal(argv[1], "probe");
    if (!probe && (argc != 5 || !g_str_equal(argv[1], "draw"))) return 2;
    gdk_set_allowed_backends("wayland");
    if (!gtk_init_check() || !gtk_layer_is_supported()) return 1;
    setlocale(LC_NUMERIC, "C");
    GdkDisplay *display = gdk_display_get_default();
    GdkMonitor *monitor = find_monitor(display, argv[2], argv[3]);
    if (!monitor) return 1;
    if (probe) {
        g_object_unref(monitor);
        return 0;
    }

    Pen pen = {0};
    if (sscanf(argv[4], "%lf,%lf,%lf,%lf", &pen.left, &pen.top, &pen.right, &pen.bottom) != 4 ||
        !(0 <= pen.left && pen.left < pen.right && pen.right <= 1 &&
          0 <= pen.top && pen.top < pen.bottom && pen.bottom <= 1)) {
        g_object_unref(monitor);
        return 2;
    }
    pen.loop = g_main_loop_new(NULL, FALSE);
    pen.strokes = g_ptr_array_new_with_free_func(g_free);
    g_signal_connect(monitor, "notify::geometry", G_CALLBACK(monitor_changed), &pen);
    GListModel *monitors = gdk_display_get_monitors(display);
    g_signal_connect(monitors, "items-changed", G_CALLBACK(output_changed), &pen);
    GtkWindow *window = GTK_WINDOW(gtk_window_new());
    gtk_window_set_decorated(window, FALSE);
    gtk_widget_add_css_class(GTK_WIDGET(window), "jaro-pen");
    GtkCssProvider *style = gtk_css_provider_new();
    gtk_css_provider_load_from_string(style, ".jaro-pen { background: transparent; box-shadow: none; }");
    gtk_style_context_add_provider_for_display(display, GTK_STYLE_PROVIDER(style), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(style);

    gtk_layer_init_for_window(window);
    gtk_layer_set_monitor(window, monitor);
    gtk_layer_set_namespace(window, "jaro-pen");
    gtk_layer_set_layer(window, GTK_LAYER_SHELL_LAYER_OVERLAY);
    gtk_layer_set_exclusive_zone(window, -1);
    gtk_layer_set_keyboard_mode(window, GTK_LAYER_SHELL_KEYBOARD_MODE_EXCLUSIVE);
    for (int edge = 0; edge < GTK_LAYER_SHELL_EDGE_ENTRY_NUMBER; edge++)
        gtk_layer_set_anchor(window, edge, TRUE);

    pen.area = gtk_drawing_area_new();
    gtk_widget_set_cursor_from_name(pen.area, "crosshair");
    gtk_window_set_child(window, pen.area);
    gtk_drawing_area_set_draw_func(GTK_DRAWING_AREA(pen.area), paint, &pen, NULL);
    g_signal_connect(pen.area, "resize", G_CALLBACK(resize), &pen);
    g_signal_connect(window, "close-request", G_CALLBACK(close_overlay), &pen);
    GtkGesture *drag = gtk_gesture_drag_new();
    gtk_gesture_single_set_button(GTK_GESTURE_SINGLE(drag), GDK_BUTTON_PRIMARY);
    g_signal_connect(drag, "drag-begin", G_CALLBACK(drag_begin), &pen);
    g_signal_connect(drag, "drag-update", G_CALLBACK(drag_update), &pen);
    g_signal_connect(drag, "drag-end", G_CALLBACK(drag_end), &pen);
    g_signal_connect(drag, "cancel", G_CALLBACK(cancel_drag), &pen);
    gtk_widget_add_controller(pen.area, GTK_EVENT_CONTROLLER(drag));
    GtkGesture *click = gtk_gesture_click_new();
    gtk_gesture_single_set_button(GTK_GESTURE_SINGLE(click), GDK_BUTTON_SECONDARY);
    g_signal_connect(click, "pressed", G_CALLBACK(secondary), &pen);
    gtk_widget_add_controller(pen.area, GTK_EVENT_CONTROLLER(click));
    GtkEventController *key = gtk_event_controller_key_new();
    g_signal_connect(key, "key-pressed", G_CALLBACK(key_pressed), &pen);
    gtk_widget_add_controller(GTK_WIDGET(window), key);
    guint parent = g_unix_fd_add(STDIN_FILENO, G_IO_HUP | G_IO_ERR | G_IO_IN, parent_closed, &pen);
    guint animation = g_timeout_add(16, animate, &pen);
    gtk_window_present(window);
    g_main_loop_run(pen.loop);
    release_stroke(&pen);
    g_source_remove(parent);
    g_source_remove(animation);
    g_signal_handlers_disconnect_by_data(monitors, &pen);
    g_signal_handlers_disconnect_by_data(monitor, &pen);
    gtk_window_destroy(window);
    g_ptr_array_unref(pen.strokes);
    g_main_loop_unref(pen.loop);
    g_object_unref(monitor);
    return 0;
}
