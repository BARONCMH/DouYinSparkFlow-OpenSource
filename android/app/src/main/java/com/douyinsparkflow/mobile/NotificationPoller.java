package com.douyinsparkflow.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class NotificationPoller {
    static final String SITE = normalizeSiteUrl(BuildConfig.SITE_URL);
    private static final String CHANNEL_ID = "send-results";
    private static final int NOTIFICATION_ID = 3107;

    private NotificationPoller() {}

    private static String normalizeSiteUrl(String value) {
        String site = value == null ? "" : value.trim();
        return site.endsWith("/") ? site : site + "/";
    }

    static boolean isTrustedOrigin(Uri uri) {
        if (uri == null) return false;
        Uri site = Uri.parse(SITE);
        return "https".equalsIgnoreCase(uri.getScheme())
                && "https".equalsIgnoreCase(site.getScheme())
                && site.getHost() != null && site.getHost().equalsIgnoreCase(uri.getHost())
                && effectivePort(site) == effectivePort(uri);
    }

    private static int effectivePort(Uri uri) {
        return uri.getPort() < 0 ? 443 : uri.getPort();
    }

    static void ensureChannel(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager != null && manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(new NotificationChannel(
                    CHANNEL_ID, "发送结果", NotificationManager.IMPORTANCE_DEFAULT));
        }
    }

    static void poll(Context context) {
        String cookie = SessionVault.readSession(context);
        if (cookie.isEmpty()) return;
        HttpURLConnection connection = null;
        try {
            URL url = new URL(Uri.parse(SITE).buildUpon().appendPath("api").appendPath("mobile")
                    .appendPath("notifications").build().toString());
            if (!isTrustedOrigin(Uri.parse(url.toString()))) return;
            connection = (HttpURLConnection) url.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(10000);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Cookie", cookie);
            if (connection.getResponseCode() != HttpURLConnection.HTTP_OK) return;
            String body = readAll(connection.getInputStream());
            JSONArray runs = new JSONObject(body).optJSONArray("runs");
            if (runs == null) return;
            if (runs.length() == 0) {
                if (!SessionVault.hasBaseline(context)) SessionVault.setLastRun(context, "__none__");
                return;
            }
            if (!SessionVault.hasBaseline(context)) {
                SessionVault.setLastRun(context, eventId(runs.optJSONObject(0)));
                return;
            }
            String last = SessionVault.lastRun(context);
            int marker = "__none__".equals(last) ? runs.length() : findEvent(runs, last);
            if (marker < 0) {
                // The saved event aged out of the bounded history; rebaseline without replaying old alerts.
                SessionVault.setLastRun(context, eventId(runs.optJSONObject(0)));
                return;
            }
            // The endpoint returns newest first. Walk the unseen items oldest first so each
            // notification is delivered in the same order the sends completed.
            for (int i = marker - 1; i >= 0; i--) {
                JSONObject run = runs.optJSONObject(i);
                if (run == null) continue;
                if (!postResult(context, run)) break;
                SessionVault.setLastRun(context, eventId(run));
            }
        } catch (Exception ignored) {
            // Network and expired-session errors are retried by the next periodic job.
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static String readAll(InputStream input) throws Exception {
        try (InputStream in = input; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = in.read(buffer)) >= 0) out.write(buffer, 0, count);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static String eventId(JSONObject run) {
        if (run == null) return "";
        String id = run.optString("event_id", "");
        if (!id.isEmpty()) return id;
        return legacyEventId(run);
    }

    private static String legacyEventId(JSONObject run) {
        if (run == null) return "";
        return run.optString("at", "") + "|" + run.optString("unique_id", "")
                + "|" + run.optString("account", "") + "|" + run.optString("status", "");
    }

    private static int findEvent(JSONArray runs, String eventId) {
        for (int i = 0; i < runs.length(); i++) {
            JSONObject run = runs.optJSONObject(i);
            if (eventId.equals(eventId(run)) || eventId.equals(legacyEventId(run))) return i;
        }
        return -1;
    }

    private static boolean postResult(Context context, JSONObject run) {
        if (Build.VERSION.SDK_INT >= 33
                && context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) return false;
        ensureChannel(context);
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null || !manager.areNotificationsEnabled()) return false;
        NotificationChannel channel = manager.getNotificationChannel(CHANNEL_ID);
        if (channel != null && channel.getImportance() == NotificationManager.IMPORTANCE_NONE) return false;

        String status = run.optString("status", "");
        String title;
        String summary;
        switch (status) {
            case "ok":
                title = "火花发送成功";
                summary = "本次续火花消息已发送成功。";
                break;
            case "partial":
                title = "部分好友发送成功";
                summary = "部分好友发送成功，请打开应用查看详情。";
                break;
            case "no_login":
                title = "发送失败：登录已失效";
                summary = "请重新登录抖音账号后再试。";
                break;
            case "no_friend":
                title = "发送失败：未找到好友";
                summary = "请检查好友名称和账号配置。";
                break;
            case "queue_timeout":
                title = "发送失败：等待超时";
                summary = "任务等待发送资源超时，请打开应用查看详情。";
                break;
            case "skipped":
                title = "本次任务未发送";
                summary = "任务已跳过，请打开应用查看原因。";
                break;
            case "error":
                title = "发送失败：任务异常";
                summary = "执行过程中发生错误，请打开应用查看详情。";
                break;
            case "failed":
                title = "火花发送失败";
                summary = "本次续火花消息未能全部送达，请打开应用查看详情。";
                break;
            default:
                title = "发送任务已结束";
                summary = "任务状态：" + (status.isEmpty() ? "未知" : status);
                break;
        }
        Intent open = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pending = PendingIntent.getActivity(context, 1, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String account = run.optString("account", "你的账号");
        String time = run.optString("at", "");
        Notification.Builder builder = new Notification.Builder(context, CHANNEL_ID);
        Notification notification = builder
                .setSmallIcon(R.drawable.ic_spark)
                .setContentTitle(title)
                .setContentText(account + (time.isEmpty() ? "" : " · " + time))
                .setStyle(new Notification.BigTextStyle().bigText(
                        summary + "\n" + account + (time.isEmpty() ? "" : " · " + time)))
                .setContentIntent(pending)
                .setAutoCancel(true)
                .build();
        String id = eventId(run);
        int notificationId = id.isEmpty() ? NOTIFICATION_ID : id.hashCode();
        if (notificationId == 0) notificationId = NOTIFICATION_ID;
        manager.notify(notificationId, notification);
        return true;
    }
}
