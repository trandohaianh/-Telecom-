package vn.lachan.fraudshield

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * Vỏ Android của app thuê bao: hiển thị giao diện /app/ của server trong WebView.
 *
 * Trình giả lập dùng 10.0.2.2 để gọi máy tính đang chạy run_demo.py; điện thoại thật dùng IP Wi-Fi
 * của máy đó (đổi trong hộp thoại "Máy chủ"). Bản demo chưa nhận cuộc gọi thật của máy: việc đó cần
 * tích hợp với nhà mạng và quyền CallScreeningService của Android.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var errorView: LinearLayout
    private lateinit var errorText: TextView
    private lateinit var progress: ProgressBar
    // true sau khi trang đầu tiên tải xong: vòng tiến trình chỉ hiện ở lần tải đầu (và khi bấm Thử lại / đổi máy chủ).
    private var firstLoadDone = false
    private val prefs by lazy { getSharedPreferences("lachan", MODE_PRIVATE) }

    private fun serverUrl(): String = prefs.getString("server", BuildConfig.DEFAULT_SERVER) ?: BuildConfig.DEFAULT_SERVER

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        swipe = findViewById(R.id.swipe)
        errorView = findViewById(R.id.error)
        errorText = findViewById(R.id.error_text)
        progress = findViewById(R.id.progress)

        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true          // phiên đăng nhập của app lưu trong localStorage
        web.settings.userAgentString = web.settings.userAgentString + " LaChanAndroid/0.1"
        web.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                errorView.visibility = View.GONE
                if (!firstLoadDone && !swipe.isRefreshing) progress.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView, url: String) {
                swipe.isRefreshing = false
                progress.visibility = View.GONE
                firstLoadDone = true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) showError(error.description.toString())
            }

            // Chỉ trang của chính server mở trong WebView (nơi giữ phiên đăng nhập trong localStorage).
            // Liên kết sang nơi khác (kể cả liên kết trong nội dung cảnh báo) mở bằng trình duyệt hệ thống.
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url
                val home = Uri.parse(serverUrl())
                if (target.scheme == home.scheme && target.host == home.host && target.port == home.port) return false
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, target).addCategory(Intent.CATEGORY_BROWSABLE))
                } catch (_: ActivityNotFoundException) {
                    // Không có ứng dụng mở được liên kết: bỏ qua, không tải nó trong WebView.
                }
                return true
            }
        }

        // Kéo xuống để tải lại, chỉ khi trang đang ở đầu (không cướp thao tác cuộn của app).
        swipe.setColorSchemeResources(R.color.brand)
        swipe.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 }
        swipe.setOnRefreshListener { web.reload() }

        findViewById<Button>(R.id.retry).setOnClickListener { firstLoadDone = false; web.loadUrl(serverUrl()) }
        findViewById<Button>(R.id.change_server).setOnClickListener { askServer() }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        if (savedInstanceState == null) web.loadUrl(serverUrl()) else web.restoreState(savedInstanceState)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    private fun showError(why: String) {
        swipe.isRefreshing = false
        progress.visibility = View.GONE
        errorText.text = getString(R.string.error_body, serverUrl(), why)
        errorView.visibility = View.VISIBLE
    }

    private fun askServer() {
        val input = EditText(this).apply {
            setSingleLine()
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setHint(R.string.server_input_hint)
            contentDescription = getString(R.string.server_input_desc)
            setText(serverUrl())
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.server_title)
            .setMessage(R.string.server_hint)
            .setView(input)
            .setPositiveButton(R.string.save) { _, _ ->
                var url = input.text.toString().trim()
                if (!url.startsWith("http")) url = (if (BuildConfig.DEBUG) "http://" else "https://") + url
                // Bản phát hành không cho HTTP (network_security_config của release cũng chặn cleartext).
                if (!BuildConfig.DEBUG && url.startsWith("http://")) url = "https://" + url.removePrefix("http://")
                if (!url.endsWith("/")) url += "/"
                if (!url.endsWith("/app/")) url += "app/"
                prefs.edit().putString("server", url).apply()
                firstLoadDone = false
                web.loadUrl(url)
            }
            .setNeutralButton(R.string.reset) { _, _ ->
                prefs.edit().remove("server").apply()
                firstLoadDone = false
                web.loadUrl(serverUrl())
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }
}
