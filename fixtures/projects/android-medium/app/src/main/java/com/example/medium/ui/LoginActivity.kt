package com.example.medium.ui

import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import androidx.appcompat.app.AppCompatActivity
import com.example.medium.R
import com.example.medium.network.ApiClient

class LoginActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_login)

        val etUsername = findViewById<EditText>(R.id.et_username)
        val etPassword = findViewById<EditText>(R.id.et_password)
        val btnLogin = findViewById<Button>(R.id.btn_login)

        btnLogin.setOnClickListener {
            val username = etUsername.text.toString()
            val password = etPassword.text.toString()
            if (username.isNotBlank() && password.isNotBlank()) {
                ApiClient.instance.login(username, password) { success ->
                    if (success) {
                        getSharedPreferences("app", MODE_PRIVATE)
                            .edit().putBoolean("logged_in", true).apply()
                        finish()
                    }
                }
            }
        }
    }
}
