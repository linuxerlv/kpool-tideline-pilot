package io.github.linuxerlv.tidelinepilot;

import android.app.Activity;
import android.graphics.Insets;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import org.json.JSONException;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final String TAG = "TidelinePilot";
    private static final String GAME_URL = "https://appassets.androidplatform.net/assets/tideline.html";
    private static final int BACKGROUND = 0xff091821;
    private static final int BOARD_CELLS = 49;
    private static final int BOARD_CHECK_ATTEMPTS = 20;
    private static final long BOARD_CHECK_INTERVAL_MS = 250;
    private static final String BOARD_READY = "(function () {"
            + "var tiles = document.querySelectorAll('#board button.tile[data-cell][aria-label]');"
            + "var boat = document.querySelector('#board .tile .boat');"
            + "return tiles.length === " + BOARD_CELLS + " && !!boat && boat.getBoundingClientRect().width > 0;"
            + "})()";

    private WebView webView;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        String html;
        String readyMessage;
        try {
            html = readAsset("tideline.html");
            JSONObject metadata = new JSONObject(readAsset("tideline-build.json"));
            readyMessage = "READY source=" + metadata.getString("source_sha")
                    + " version=" + metadata.getInt("version_code") + " cells=" + BOARD_CELLS;
        } catch (IOException | JSONException error) {
            Log.e(TAG, "Could not load bundled Tideline game", error);
            TextView message = new TextView(this);
            message.setText("无法载入潮汐邮差。");
            setContentView(message);
            return;
        }

        WebView view = new WebView(this);
        view.setBackgroundColor(BACKGROUND);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setBlockNetworkLoads(true);

        view.setWebViewClient(new WebViewClient() {
            private boolean checkingBoard;

            @Override
            public boolean shouldOverrideUrlLoading(WebView ignored, WebResourceRequest request) {
                return true;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView ignored, WebResourceRequest request) {
                // The standalone bundle loads its game engine from an inline data URL.
                if ("data".equals(request.getUrl().getScheme())) {
                    return null;
                }
                return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden",
                        Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
            }

            @Override
            public void onPageFinished(WebView loadedView, String url) {
                if (loadedView == webView && GAME_URL.equals(url) && !checkingBoard) {
                    checkingBoard = true;
                    checkBoard(loadedView, readyMessage, BOARD_CHECK_ATTEMPTS);
                }
            }
        });

        FrameLayout content = new FrameLayout(this);
        content.setBackgroundColor(BACKGROUND);
        if (Build.VERSION.SDK_INT >= 35) {
            content.setOnApplyWindowInsetsListener((container, insets) -> {
                Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                container.setPadding(safe.left, safe.top, safe.right, safe.bottom);
                return WindowInsets.CONSUMED;
            });
        }
        content.addView(view, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        setContentView(content);
        webView = view;
        view.loadDataWithBaseURL(GAME_URL, html, "text/html", "UTF-8", GAME_URL);
    }

    private String readAsset(String name) throws IOException {
        try (InputStream input = getAssets().open(name)) {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int count;
            while ((count = input.read(buffer)) != -1) {
                bytes.write(buffer, 0, count);
            }
            return new String(bytes.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private void checkBoard(WebView view, String readyMessage, int attemptsLeft) {
        if (view != webView) {
            return;
        }
        view.evaluateJavascript(BOARD_READY, result -> {
            if (view != webView) {
                return;
            }
            if ("true".equals(result)) {
                view.postVisualStateCallback(0, new WebView.VisualStateCallback() {
                    @Override
                    public void onComplete(long requestId) {
                        if (view == webView) {
                            Log.i(TAG, readyMessage);
                        }
                    }
                });
            } else if (attemptsLeft > 1) {
                view.postDelayed(() -> checkBoard(view, readyMessage, attemptsLeft - 1), BOARD_CHECK_INTERVAL_MS);
            } else {
                Log.e(TAG, "The bundled game board did not render");
            }
        });
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            WebView view = webView;
            webView = null;
            ((ViewGroup) view.getParent()).removeView(view);
            view.destroy();
        }
        super.onDestroy();
    }
}
