package com.douyinsparkflow.mobile;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.annotation.SuppressLint;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.DownloadListener;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;


public final class MainActivity extends Activity {
    private static final int NOTIFICATION_PERMISSION_REQUEST = 44;
    private static final int NOTIFICATION_JOB_ID = 78031;
    private WebView webView;
    private Button notificationButton;
    private Button accountButton;
    private boolean loggingOut;
    private final ExecutorService pollExecutor = Executors.newSingleThreadExecutor();
    private final Handler pollingHandler = new Handler(Looper.getMainLooper());
    private final Runnable foregroundPoll = new Runnable() {
        @Override public void run() {
            pollNotifications();
            pollingHandler.postDelayed(this, 60_000L);
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(242, 247, 246));
        getWindow().setNavigationBarColor(Color.rgb(16, 33, 38));
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        buildLayout();
        configureWebView();
        NotificationPoller.ensureChannel(this);
        scheduleNotificationChecks();
        if (state == null) webView.loadUrl(NotificationPoller.SITE);
        else webView.restoreState(state);
        updateNotificationButton();
    }

    private void buildLayout() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(242, 247, 246));

        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(16), dp(8), dp(12), dp(8));
        bar.setBackgroundColor(Color.rgb(16, 45, 52));

        TextView title = new TextView(this);
        title.setText("✦  续火花");
        title.setTextColor(Color.WHITE);
        title.setTextSize(17);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        bar.addView(title, new LinearLayout.LayoutParams(0, dp(44), 1f));

        notificationButton = new Button(this);
        notificationButton.setTextColor(Color.WHITE);
        notificationButton.setTextSize(12);
        notificationButton.setAllCaps(false);
        notificationButton.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(8, 127, 140)));
        notificationButton.setOnClickListener(v -> requestNotificationPermission());
        bar.addView(notificationButton, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(42)));

        accountButton = new Button(this);
        accountButton.setText("切换账号");
        accountButton.setTextColor(Color.WHITE);
        accountButton.setTextSize(11);
        accountButton.setAllCaps(false);
        accountButton.setPadding(dp(8), 0, dp(8), 0);
        accountButton.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(41, 70, 76)));
        accountButton.setOnClickListener(v -> confirmAccountSwitch());
        LinearLayout.LayoutParams accountParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, dp(42));
        accountParams.leftMargin = dp(6);
        bar.addView(accountButton, accountParams);

        webView = new WebView(this);
        root.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(60)));
        root.addView(webView, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        if (Build.VERSION.SDK_INT >= 35) {
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets systemBars = insets.getInsets(android.view.WindowInsets.Type.systemBars());
                root.setPadding(0, systemBars.top, 0, systemBars.bottom);
                return insets.consumeSystemWindowInsets();
            });
        }
        setContentView(root);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        // The existing dashboard requires JavaScript. Keep it inside the exact HTTPS origin and expose no native JS bridge.
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true);
        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebChromeClient(new WebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri uri = Uri.parse(url);
            if (!isTrustedOrigin(uri) || !uri.getPath().equals("/downloads/DouYinSparkFlow.apk")) {
                Toast.makeText(this, "已阻止不受信任的下载链接", Toast.LENGTH_SHORT).show();
                return;
            }
            DownloadManager.Request request = new DownloadManager.Request(uri)
                    .setTitle("DouYinSparkFlow.apk")
                    .setDescription("下载续火花 Android 安装包")
                    .setMimeType("application/vnd.android.package-archive")
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "DouYinSparkFlow.apk");
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null && !cookie.isEmpty()) request.addRequestHeader("Cookie", cookie);
            DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            if (manager != null) {
                manager.enqueue(request);
                Toast.makeText(this, "正在下载，完成后可从系统通知打开", Toast.LENGTH_LONG).show();
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isTrustedOrigin(uri)) return false;
                if ("https".equalsIgnoreCase(uri.getScheme())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
                }
                return true;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (isTrustedOrigin(request.getUrl())) return super.shouldInterceptRequest(view, request);
                return new WebResourceResponse("text/plain", "UTF-8", null);
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler,
                                          android.net.http.SslError error) {
                handler.cancel();
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!isTrustedUrl(url)) return;
                CookieManager manager = CookieManager.getInstance();
                manager.flush();
                if (loggingOut && "/login".equals(Uri.parse(url).getPath())) {
                    loggingOut = false;
                    SessionVault.clearAccountState(getApplicationContext());
                    manager.removeAllCookies(value -> manager.flush());
                    webView.clearHistory();
                    if (accountButton != null) accountButton.setEnabled(false);
                    updateNotificationButton();
                    return;
                }
                String cookie = manager.getCookie(NotificationPoller.SITE);
                boolean hasSession = hasNonEmptySid(cookie);
                if (accountButton != null) accountButton.setEnabled(hasSession);
                if (hasSession) SessionVault.saveSession(getApplicationContext(), cookie);
                else SessionVault.clearAccountState(getApplicationContext());
                if (hasSession) pollNotifications();
                updateNotificationButton();
            }
        });
    }

    private boolean hasNonEmptySid(String cookie) {
        if (cookie == null) return false;
        for (String part : cookie.split(";")) {
            String value = part.trim();
            int equals = value.indexOf('=');
            if (equals > 0 && "sid".equals(value.substring(0, equals).trim())
                    && !value.substring(equals + 1).trim().isEmpty()) return true;
        }
        return false;
    }

    private void confirmAccountSwitch() {
        new AlertDialog.Builder(this)
                .setTitle("退出并切换账号")
                .setMessage("将安全退出当前账号，并返回登录页。你可以登录其他账号或注册新账号。")
                .setNegativeButton("取消", null)
                .setPositiveButton("退出并切换", (dialog, which) -> {
                    loggingOut = true;
                    SessionVault.clearAccountState(getApplicationContext());
                    if (accountButton != null) accountButton.setEnabled(false);
                    webView.loadUrl(NotificationPoller.SITE + "logout");
                })
                .show();
    }

    private void pollNotifications() {
        if (pollExecutor.isShutdown()) return;
        try {
            pollExecutor.execute(() -> NotificationPoller.poll(getApplicationContext()));
        } catch (java.util.concurrent.RejectedExecutionException ignored) { }
    }

    private boolean isTrustedUrl(String value) {
        try { return isTrustedOrigin(Uri.parse(value)); } catch (Exception ignored) { return false; }
    }

    private boolean isTrustedOrigin(Uri uri) {
        return NotificationPoller.isTrustedOrigin(uri);
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION_REQUEST);
        } else if (!systemNotificationsAllowed()) {
            new AlertDialog.Builder(this)
                    .setTitle("发送提醒未开启")
                    .setMessage("请在系统设置中允许续火花发送结果通知。")
                    .setNegativeButton("稍后", null)
                    .setPositiveButton("打开设置", (dialog, which) -> {
                        Intent settings = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                                .putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                        try { startActivity(settings); }
                        catch (Exception ignored) {
                            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                    Uri.parse("package:" + getPackageName())));
                        }
                    })
                    .show();
        } else {
            scheduleNotificationChecks();
            Toast.makeText(this, "发送提醒已开启", Toast.LENGTH_SHORT).show();
            pollNotifications();
            updateNotificationButton();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTIFICATION_PERMISSION_REQUEST) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                scheduleNotificationChecks();
                Toast.makeText(this, "发送提醒已开启", Toast.LENGTH_SHORT).show();
                pollNotifications();
            } else Toast.makeText(this, "你可以稍后在系统设置中开启通知", Toast.LENGTH_LONG).show();
            updateNotificationButton();
        }
    }

    private void scheduleNotificationChecks() {
        JobScheduler scheduler = (JobScheduler) getSystemService(JOB_SCHEDULER_SERVICE);
        if (scheduler == null) return;
        ComponentName service = new ComponentName(this, NotificationJob.class);
        JobInfo job = new JobInfo.Builder(NOTIFICATION_JOB_ID, service)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(15 * 60 * 1000L, 5 * 60 * 1000L)
                .setPersisted(false)
                .build();
        scheduler.schedule(job);
    }

    private void updateNotificationButton() {
        if (notificationButton == null) return;
        notificationButton.setText(systemNotificationsAllowed() ? "发送提醒已开" : "开启发送提醒");
    }

    private boolean systemNotificationsAllowed() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        android.app.NotificationManager manager = getSystemService(android.app.NotificationManager.class);
        if (manager == null || !manager.areNotificationsEnabled()) return false;
        android.app.NotificationChannel channel = manager.getNotificationChannel("send-results");
        if (channel != null && channel.getImportance() == android.app.NotificationManager.IMPORTANCE_NONE) return false;
        return true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        pollNotifications();
        pollingHandler.removeCallbacks(foregroundPoll);
        pollingHandler.postDelayed(foregroundPoll, 60_000L);
    }

    @Override
    protected void onPause() {
        pollingHandler.removeCallbacks(foregroundPoll);
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        pollingHandler.removeCallbacks(foregroundPoll);
        pollExecutor.shutdownNow();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
