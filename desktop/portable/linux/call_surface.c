#include <gtk/gtk.h>
#include <gtk4-layer-shell.h>
#include <gdk/x11/gdkx.h>
#include <stdio.h>
#include <string.h>

static const char *const bus = "org.freedesktop.Notifications";
static const char *const path = "/org/freedesktop/Notifications";
static guint32 notification;
static gboolean notification_closed;
static GMainLoop *loop;
static GtkLabel *detail;
static const char *mode;
static const char *action;
static gint64 started;
static int seconds;
static gboolean allow_always;
static GtkWindow *annotation_window;
static gboolean annotation_layer, annotation_held, annotation_shown;
static gint64 annotation_remaining;

static gboolean annotation_send(void) {
    return !strcmp(mode, "annotation-send");
}

static gboolean annotation_panel(void) {
    return annotation_send() || !strcmp(mode, "annotation-undo") || !strcmp(mode, "annotation-result");
}

static void announce(const char *message) {
    puts(message);
    fflush(stdout);
}

static gboolean annotation_visible(void) {
    if (!annotation_window || !gtk_widget_get_mapped(GTK_WIDGET(annotation_window))) return FALSE;
    GdkSurface *surface = gtk_native_get_surface(GTK_NATIVE(annotation_window));
    GdkToplevelState state = gdk_toplevel_get_state(GDK_TOPLEVEL(surface));
    if (state & (GDK_TOPLEVEL_STATE_MINIMIZED | GDK_TOPLEVEL_STATE_SUSPENDED)) return FALSE;
    return annotation_layer || (state & GDK_TOPLEVEL_STATE_FOCUSED);
}

static void show_annotation_countdown(void) {
    if (!annotation_shown && annotation_visible()) {
        annotation_shown = TRUE;
        started = g_get_monotonic_time();
        announce("shown");
        if (annotation_held) announce("hold");
    }
}

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
    if (!strcmp(mode, "screen")) return "Recording screen";
    if (annotation_send()) return "Send annotation to OpenCode";
    if (!strcmp(mode, "annotation-undo")) return "Queued in OpenCode";
    if (!strcmp(mode, "annotation-result")) return "Annotation saved in Sessions";
    return "Recording meeting";
}

static gboolean tick(gpointer data) {
    (void)data;
    int elapsed = (int)((g_get_monotonic_time() - started) / G_USEC_PER_SEC);
    char label[96];
    if (annotation_send()) {
        show_annotation_countdown();
        gint64 left = annotation_remaining - (g_get_monotonic_time() - started);
        if (!annotation_shown && !annotation_layer) g_snprintf(label, sizeof(label), "Activate this window to start the countdown");
        else if (annotation_held) g_snprintf(label, sizeof(label), "Automatic send paused");
        else if (left > 0) g_snprintf(label, sizeof(label), "Sending to OpenCode in %d…", (int)((left + G_USEC_PER_SEC - 1) / G_USEC_PER_SEC));
        else g_snprintf(label, sizeof(label), "Verifying the OpenCode destination…");
    } else if (!strcmp(mode, "annotation-undo")) {
        if (!annotation_held && g_get_monotonic_time() - started >= annotation_remaining) g_main_loop_quit(loop);
        return G_SOURCE_CONTINUE;
    } else if (countdown())
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
    if (annotation_send() && (event & G_IO_IN)) {
        gchar *line = NULL;
        gsize size = 0;
        GIOStatus status = g_io_channel_read_line(pipe, &line, &size, NULL, NULL);
        if (status == G_IO_STATUS_NORMAL && size == 45 && g_str_has_prefix(line, "confirm ")) {
            line[44] = 0;
            printf("confirm %s %s\n", line + 8, !annotation_visible() ? "unavailable" : annotation_held ? "held" : "ready");
            fflush(stdout);
        }
        g_free(line);
        if (status == G_IO_STATUS_NORMAL || status == G_IO_STATUS_AGAIN) return G_SOURCE_CONTINUE;
    }
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

static void annotation_pointer(gboolean held) {
    if (held == annotation_held) return;
    annotation_held = held;
    gint64 now = g_get_monotonic_time();
    if (held) annotation_remaining = MAX(0, annotation_remaining - (now - started));
    else annotation_remaining = MAX(G_USEC_PER_SEC, annotation_remaining);
    started = now;
    if (annotation_shown) announce(held ? "hold" : "resume");
}

static void annotation_enter(GtkEventControllerMotion *motion, double x, double y, gpointer data) {
    (void)motion; (void)x; (void)y; (void)data;
    annotation_pointer(TRUE);
}

static void annotation_leave(GtkEventControllerMotion *motion, gpointer data) {
    (void)motion; (void)data;
    annotation_pointer(FALSE);
}

static gboolean annotation_closed(GtkWindow *window, gpointer data) {
    (void)window; (void)data;
    action = "cancel";
    g_main_loop_quit(loop);
    return TRUE;
}

static gboolean annotation_key(GtkEventControllerKey *keys, guint key, guint code, GdkModifierType modifiers, gpointer data) {
    (void)keys; (void)code; (void)modifiers; (void)data;
    return key == GDK_KEY_Escape && annotation_closed(annotation_window, NULL);
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

static void show_capsule(const char *name, gboolean x11, gboolean layer_shell) {
    GtkWindow *window = GTK_WINDOW(gtk_window_new());
    gboolean screen = !strcmp(mode, "screen");
    gboolean receipt = !strcmp(mode, "annotation-undo"), finished = !strcmp(mode, "annotation-result");
    const char *title = screen ? "Jaro Screen Recording" : annotation_send() ? "Jaro Automatic Send" :
        receipt || finished ? "Jaro Annotation Delivery" : "Jaro Meeting Controls";
    gtk_window_set_title(window, title);
    gtk_window_set_decorated(window, FALSE);
    gtk_window_set_resizable(window, FALSE);
    gtk_window_set_default_size(window, 328, screen || finished || !strcmp(mode, "saving") ? 76 : 118);
    gtk_widget_set_focusable(GTK_WIDGET(window), FALSE);
    gtk_widget_add_css_class(GTK_WIDGET(window), "jaro-call-surface");
    if (screen) gtk_widget_add_css_class(GTK_WIDGET(window), "jaro-screen-recording");
    GtkCssProvider *css = gtk_css_provider_new();
    gtk_css_provider_load_from_string(css,
        "window.jaro-call-surface { background: transparent; }"
        ".call-card { background: #202029; border: 1px solid #464453; border-radius: 15px; padding: 14px; color: #f0eef6; }"
        ".call-name { font-weight: bold; font-size: 16px; }"
        ".call-detail { color: #b5b1c1; }"
        ".jaro-screen-recording .call-detail { color: #ffb6b1; font-weight: bold; }"
        ".call-card button { padding: 3px 11px; background: #30303c; color: #f0eef6; border: 1px solid #494858;"
        " border-radius: 9px; box-shadow: none; text-shadow: none; }"
        ".call-card button.primary { background: #ded6ff; color: #252238; border-color: #ded6ff; }"
        ".call-card button:hover { opacity: .86; }");
    gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(css),
                                               GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(css);
    if (layer_shell) {
        gtk_layer_init_for_window(window);
        gtk_layer_set_namespace(window, annotation_panel() ? "jaro-annotation-send" : "jaro-meeting-controls");
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
    if (!screen) {
        GtkWidget *heading = gtk_label_new(name);
        gtk_widget_add_css_class(heading, "call-name");
        gtk_label_set_xalign(GTK_LABEL(heading), 0);
        if (annotation_panel()) {
            gtk_label_set_ellipsize(GTK_LABEL(heading), PANGO_ELLIPSIZE_END);
            gtk_label_set_max_width_chars(GTK_LABEL(heading), 32);
        }
        gtk_box_append(GTK_BOX(card), heading);
    }
    detail = GTK_LABEL(gtk_label_new(summary()));
    gtk_widget_add_css_class(GTK_WIDGET(detail), "call-detail");
    gtk_label_set_xalign(detail, 0);
    gtk_box_append(GTK_BOX(card), GTK_WIDGET(detail));
    GtkBox *buttons = GTK_BOX(gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 7));
    gtk_widget_set_halign(GTK_WIDGET(buttons), GTK_ALIGN_END);
    gtk_widget_set_margin_top(GTK_WIDGET(buttons), 8);
    gboolean timer = FALSE;
    if (annotation_send() || receipt) {
        annotation_remaining = (gint64)seconds * G_USEC_PER_SEC;
        GtkEventController *motion = gtk_event_controller_motion_new();
        g_signal_connect(motion, "enter", G_CALLBACK(annotation_enter), NULL);
        g_signal_connect(motion, "leave", G_CALLBACK(annotation_leave), NULL);
        gtk_widget_add_controller(card, motion);
        g_signal_connect(window, "close-request", G_CALLBACK(annotation_closed), NULL);
        timer = TRUE;
    }
    if (annotation_send()) {
        annotation_window = window;
        annotation_layer = x11 || layer_shell;
        add_button(buttons, "Cancel send", "cancel", FALSE);
        if (!annotation_layer) {
            GtkEventController *keyboard = gtk_event_controller_key_new();
            g_signal_connect(keyboard, "key-pressed", G_CALLBACK(annotation_key), NULL);
            gtk_widget_add_controller(GTK_WIDGET(window), keyboard);
            gtk_widget_set_focusable(gtk_widget_get_first_child(GTK_WIDGET(buttons)), TRUE);
        }
    } else if (receipt) {
        add_button(buttons, "Undo send", "undo", TRUE);
    } else if (!strcmp(mode, "offer")) {
        add_button(buttons, "Options", "more", FALSE);
        add_button(buttons, "Not now", "dismiss", FALSE);
        add_button(buttons, "Record", "record", TRUE);
    } else if (options()) {
        add_button(buttons, "Cancel", "back", FALSE);
        add_button(buttons, "Never offer", "never", FALSE);
        if (allow_always) add_button(buttons, "Always record", "always", TRUE);
    } else if (!finished && strcmp(mode, "saving")) {
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
    if (annotation_send() && !annotation_layer) gtk_window_present(window);
    else gtk_widget_set_visible(GTK_WIDGET(window), TRUE);
    if (annotation_send()) show_annotation_countdown();
    if (screen && gtk_widget_get_mapped(GTK_WIDGET(window))) {
        puts("ready");
        fflush(stdout);
    }
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

static void closed(GDBusConnection *connection, const gchar *sender, const gchar *object,
                   const gchar *interface, const gchar *signal, GVariant *parameters, gpointer data) {
    (void)connection; (void)sender; (void)object; (void)interface; (void)signal; (void)data;
    guint32 id, reason;
    g_variant_get(parameters, "(uu)", &id, &reason);
    if (id != notification) return;
    notification_closed = TRUE;
    if (!action && options()) action = "back";
    if (!action && reason == 2 && !strcmp(mode, "offer")) action = "dismiss";
    g_main_loop_quit(loop);
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
    guint close_subscription = g_dbus_connection_signal_subscribe(connection, bus, bus, "NotificationClosed",
        path, NULL, G_DBUS_SIGNAL_FLAGS_NONE, closed, NULL, NULL);
    GVariant *reply = g_dbus_connection_call_sync(connection, bus, path, bus, "Notify",
        g_variant_new("(susssasa{sv}i)", "Jaro", 0u, "local.jardaudio.desktop", title, body,
                      &actions, &hints, timeout),
        G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 1500, NULL, NULL);
    g_free(body);
    if (reply) {
        g_variant_get(reply, "(u)", &notification);
        g_variant_unref(reply);
        if (notification) g_main_loop_run(loop);
        if (notification && !notification_closed) {
            reply = g_dbus_connection_call_sync(connection, bus, path, bus, "CloseNotification",
                g_variant_new("(u)", notification), NULL, G_DBUS_CALL_FLAGS_NONE, 600, NULL, NULL);
            if (reply) g_variant_unref(reply);
        }
    }
    g_dbus_connection_signal_unsubscribe(connection, subscription);
    g_dbus_connection_signal_unsubscribe(connection, close_subscription);
    g_object_unref(connection);
    return notification != 0;
}

int main(int argc, char **argv) {
    if (argc != 4 && argc != 5) return 2;
    mode = argv[1];
    gboolean screen = !strcmp(mode, "screen");
    if (!screen && !annotation_panel() && strcmp(mode, "offer") && strcmp(mode, "recording") && strcmp(mode, "automatic") &&
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
    if (screen && !x11) return 3;
    gboolean layer_shell = visible && !x11 && gtk_layer_is_supported();
    if (visible && (x11 || layer_shell || annotation_panel())) show_capsule(argv[2], x11, layer_shell);
    else if (annotation_panel()) visible = FALSE;
    else visible = show_notification(argv[2]);
    if (action) puts(action);
    g_main_loop_unref(loop);
    return visible ? 0 : 3;
}
