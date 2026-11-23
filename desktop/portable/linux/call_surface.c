#include <gtk/gtk.h>
#include <gtk4-layer-shell.h>
#include <gdk/x11/gdkx.h>
#include <stdio.h>
#include <string.h>

static const char *const bus = "org.freedesktop.Notifications";
static const char *const path = "/org/freedesktop/Notifications";
static guint32 notification;
static GMainLoop *loop;
static GtkLabel *detail;
static const char *mode;
static const char *action;
static gint64 started;
static int seconds;
static gboolean allow_always;

static gboolean countdown(void) {
    return !strcmp(mode, "ending") || !strcmp(mode, "switching");
}

static gboolean options(void) {
    return !strcmp(mode, "offer-options");
}

static const char *summary(void) {
    if (!strcmp(mode, "offer")) return "Call detected";
    if (options()) return "Change anytime in Settings";
    if (!strcmp(mode, "ending")) return "Call ended";
    if (!strcmp(mode, "switching")) return "Call changed";
    if (!strcmp(mode, "saving")) return "Saving meeting";
    if (!strcmp(mode, "automatic")) return "Recording automatically";
    return "Recording meeting";
}

static gboolean tick(gpointer data) {
    (void)data;
    int elapsed = (int)((g_get_monotonic_time() - started) / G_USEC_PER_SEC);
    char label[96];
    if (countdown())
        g_snprintf(label, sizeof(label), "%s · Stops in %d seconds", summary(), MAX(1, seconds - elapsed));
    else {
        int time = seconds + elapsed;
        g_snprintf(label, sizeof(label), "%s · %d:%02d", summary(), time / 60, time % 60);
    }
    gtk_label_set_text(detail, label);
    return G_SOURCE_CONTINUE;
}

static gboolean disconnected(GIOChannel *pipe, GIOCondition event, gpointer data) {
    (void)pipe; (void)event; (void)data;
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

static void clicked(GtkButton *button, gpointer choice) {
    (void)button;
    action = choice;
    g_main_loop_quit(loop);
}

static void add_button(GtkBox *buttons, const char *label, const char *choice, gboolean primary) {
    GtkWidget *button = gtk_button_new_with_label(label);
    gtk_widget_set_focusable(button, FALSE);
    if (primary) gtk_widget_add_css_class(button, "primary");
    g_signal_connect(button, "clicked", G_CALLBACK(clicked), (gpointer)choice);
    gtk_box_append(buttons, button);
}

static void place_x11(GdkSurface *surface) {
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    Display *display = gdk_x11_display_get_xdisplay(gdk_surface_get_display(surface));
    Window window = gdk_x11_surface_get_xid(surface);
    G_GNUC_END_IGNORE_DEPRECATIONS
    XSetWindowAttributes attributes = {.override_redirect = True};
    XChangeWindowAttributes(display, window, CWOverrideRedirect, &attributes);
    XWMHints hints = {.flags = InputHint, .input = False};
    XSetWMHints(display, window, &hints);
    Window root = DefaultRootWindow(display), pointer_root, child;
    int x = 0, y = 0, unused_x, unused_y;
    unsigned int mask;
    XQueryPointer(display, root, &pointer_root, &child, &x, &y, &unused_x, &unused_y, &mask);
    int scale = gdk_surface_get_scale_factor(surface);
    int right = DisplayWidth(display, DefaultScreen(display)), top = 0;
    GListModel *monitors = gdk_display_get_monitors(gdk_surface_get_display(surface));
    for (guint i = 0; i < g_list_model_get_n_items(monitors); i++) {
        GdkMonitor *monitor = g_list_model_get_item(monitors, i);
        GdkRectangle box;
        gdk_monitor_get_geometry(monitor, &box);
        g_object_unref(monitor);
        if (x / scale < box.x || x / scale >= box.x + box.width ||
            y / scale < box.y || y / scale >= box.y + box.height) continue;
        right = (box.x + box.width) * scale;
        top = box.y * scale;
        break;
    }
    XMoveWindow(display, window, right - 352 * scale, top + 48 * scale);
    XFlush(display);
}

static void show_capsule(const char *name, gboolean x11) {
    GtkWindow *window = GTK_WINDOW(gtk_window_new());
    gtk_window_set_title(window, "Jaro Meeting Controls");
    gtk_window_set_decorated(window, FALSE);
    gtk_window_set_resizable(window, FALSE);
    gtk_window_set_default_size(window, 328, !strcmp(mode, "saving") ? 76 : 118);
    gtk_widget_set_focusable(GTK_WIDGET(window), FALSE);
    gtk_widget_add_css_class(GTK_WIDGET(window), "jaro-call-surface");
    GtkCssProvider *css = gtk_css_provider_new();
    gtk_css_provider_load_from_string(css,
        "window.jaro-call-surface { background: transparent; }"
        ".call-card { background: #202029; border: 1px solid #464453; border-radius: 15px; padding: 14px; color: #f0eef6; }"
        ".call-name { font-weight: bold; font-size: 16px; }"
        ".call-detail { color: #b5b1c1; }"
        ".call-card button { padding: 3px 11px; background: #30303c; color: #f0eef6; border: 1px solid #494858;"
        " border-radius: 9px; box-shadow: none; text-shadow: none; }"
        ".call-card button.primary { background: #ded6ff; color: #252238; border-color: #ded6ff; }"
        ".call-card button:hover { opacity: .86; }");
    gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(css),
                                               GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(css);
    if (!x11) {
        gtk_layer_init_for_window(window);
        gtk_layer_set_namespace(window, "jaro-meeting-controls");
        gtk_layer_set_layer(window, GTK_LAYER_SHELL_LAYER_OVERLAY);
        gtk_layer_set_keyboard_mode(window, GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
        gtk_layer_set_anchor(window, GTK_LAYER_SHELL_EDGE_TOP, TRUE);
        gtk_layer_set_anchor(window, GTK_LAYER_SHELL_EDGE_RIGHT, TRUE);
        gtk_layer_set_margin(window, GTK_LAYER_SHELL_EDGE_TOP, 38);
        gtk_layer_set_margin(window, GTK_LAYER_SHELL_EDGE_RIGHT, 24);
    }
    GtkWidget *card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 4);
    gtk_widget_add_css_class(card, "call-card");
    gtk_window_set_child(window, card);
    GtkWidget *heading = gtk_label_new(name);
    gtk_widget_add_css_class(heading, "call-name");
    gtk_label_set_xalign(GTK_LABEL(heading), 0);
    gtk_box_append(GTK_BOX(card), heading);
    detail = GTK_LABEL(gtk_label_new(summary()));
    gtk_widget_add_css_class(GTK_WIDGET(detail), "call-detail");
    gtk_label_set_xalign(detail, 0);
    gtk_box_append(GTK_BOX(card), GTK_WIDGET(detail));
    GtkBox *buttons = GTK_BOX(gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 7));
    gtk_widget_set_halign(GTK_WIDGET(buttons), GTK_ALIGN_END);
    gtk_widget_set_margin_top(GTK_WIDGET(buttons), 8);
    gboolean timer = FALSE;
    if (!strcmp(mode, "offer")) {
        add_button(buttons, "Options", "more", FALSE);
        add_button(buttons, "Not now", "dismiss", FALSE);
        add_button(buttons, "Record", "record", TRUE);
    } else if (options()) {
        add_button(buttons, "Cancel", "back", FALSE);
        add_button(buttons, "Never offer", "never", FALSE);
        if (allow_always) add_button(buttons, "Always record", "always", TRUE);
    } else if (strcmp(mode, "saving")) {
        timer = TRUE;
        if (countdown()) add_button(buttons, "Keep recording", "keep", TRUE);
        add_button(buttons, "Stop & Save", "stop", FALSE);
    }
    if (gtk_widget_get_first_child(GTK_WIDGET(buttons))) gtk_box_append(GTK_BOX(card), GTK_WIDGET(buttons));
    else g_object_unref(g_object_ref_sink(buttons));
    gtk_widget_realize(GTK_WIDGET(window));
    if (x11) place_x11(gtk_native_get_surface(GTK_NATIVE(window)));
    if (timer) tick(NULL);
    guint timer_id = timer ? g_timeout_add(250, tick, NULL) : 0;
    gtk_widget_set_visible(GTK_WIDGET(window), TRUE);
    g_main_loop_run(loop);
    if (timer_id) g_source_remove(timer_id);
    gtk_window_destroy(window);
}

static void invoked(GDBusConnection *connection, const gchar *sender, const gchar *object,
                    const gchar *interface, const gchar *signal, GVariant *parameters, gpointer data) {
    (void)connection; (void)sender; (void)object; (void)interface; (void)signal; (void)data;
    guint32 id;
    const char *choice;
    g_variant_get(parameters, "(u&s)", &id, &choice);
    if (id != notification) return;
    if (!strcmp(choice, "record")) action = "record";
    else if (!strcmp(choice, "dismiss")) action = "dismiss";
    else if (!strcmp(choice, "more")) action = "more";
    else if (!strcmp(choice, "back")) action = "back";
    else if (!strcmp(choice, "never")) action = "never";
    else if (!strcmp(choice, "always")) action = "always";
    else if (!strcmp(choice, "keep")) action = "keep";
    else if (!strcmp(choice, "stop")) action = "stop";
    if (action) g_main_loop_quit(loop);
}

static void notification_button(GVariantBuilder *buttons, const char *action_id, const char *label) {
    g_variant_builder_add(buttons, "s", action_id);
    g_variant_builder_add(buttons, "s", label);
}

static gboolean show_notification(const char *name) {
    if (!strcmp(mode, "saving")) return TRUE;
    GDBusConnection *connection = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, NULL);
    if (!connection) return FALSE;
    GVariantBuilder hints, actions;
    g_variant_builder_init(&hints, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&hints, "{sv}", "desktop-entry", g_variant_new_string("local.jardaudio.desktop"));
    g_variant_builder_init(&actions, G_VARIANT_TYPE_STRING_ARRAY);
    gboolean offer = !strcmp(mode, "offer"), ending = countdown();
    if (offer) {
        notification_button(&actions, "record", "Record");
        notification_button(&actions, "dismiss", "Not now");
        notification_button(&actions, "more", "Options");
    } else if (options()) {
        if (allow_always) notification_button(&actions, "always", "Always record");
        notification_button(&actions, "never", "Never offer");
        notification_button(&actions, "back", "Cancel");
    } else {
        notification_button(&actions, "stop", "Stop & Save");
        if (ending) notification_button(&actions, "keep", "Keep recording");
    }
    const char *title = options() ? name : summary();
    char *body = ending ? g_strdup_printf("%s · Stops in %d seconds", name, seconds) : g_strdup(options() ? summary() : name);
    int timeout = offer || ending ? seconds * 1000 : options() ? 0 : -1;
    guint subscription = g_dbus_connection_signal_subscribe(connection, bus, bus, "ActionInvoked",
        path, NULL, G_DBUS_SIGNAL_FLAGS_NONE, invoked, NULL, NULL);
    GVariant *reply = g_dbus_connection_call_sync(connection, bus, path, bus, "Notify",
        g_variant_new("(susssasa{sv}i)", "Jaro", 0u, "local.jardaudio.desktop", title, body,
                      &actions, &hints, timeout),
        G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 1500, NULL, NULL);
    g_free(body);
    if (reply) {
        g_variant_get(reply, "(u)", &notification);
        g_variant_unref(reply);
        g_main_loop_run(loop);
        reply = g_dbus_connection_call_sync(connection, bus, path, bus, "CloseNotification",
            g_variant_new("(u)", notification), NULL, G_DBUS_CALL_FLAGS_NONE, 600, NULL, NULL);
        if (reply) g_variant_unref(reply);
    }
    g_dbus_connection_signal_unsubscribe(connection, subscription);
    g_object_unref(connection);
    return notification != 0;
}

int main(int argc, char **argv) {
    if (argc != 4 && argc != 5) return 2;
    mode = argv[1];
    if (strcmp(mode, "offer") && strcmp(mode, "recording") && strcmp(mode, "automatic") &&
        !countdown() && !options() && strcmp(mode, "saving")) return 2;
    if (argc == 5 && (!options() || strcmp(argv[4], "always"))) return 2;
    allow_always = argc == 5;
    gint64 value;
    if (!g_ascii_string_to_signed(argv[3], 10, 0, 86400, &value, NULL)) return 2;
    seconds = (int)value;
    started = g_get_monotonic_time();
    loop = g_main_loop_new(NULL, FALSE);
    GIOChannel *pipe = g_io_channel_unix_new(0);
    g_io_add_watch(pipe, G_IO_IN | G_IO_HUP | G_IO_ERR | G_IO_NVAL, disconnected, NULL);
    g_io_channel_unref(pipe);
    gboolean visible = gtk_init_check();
    gboolean x11 = visible && GDK_IS_X11_DISPLAY(gdk_display_get_default());
    if (visible && (x11 || gtk_layer_is_supported())) show_capsule(argv[2], x11);
    else visible = show_notification(argv[2]);
    if (action) puts(action);
    g_main_loop_unref(loop);
    return visible ? 0 : 3;
}
