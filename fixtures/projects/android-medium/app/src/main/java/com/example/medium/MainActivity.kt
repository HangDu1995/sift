package com.example.medium

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.example.medium.ui.LoginActivity

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val isLoggedIn = getSharedPreferences("app", MODE_PRIVATE)
            .getBoolean("logged_in", false)

        if (!isLoggedIn) {
            startActivity(android.content.Intent(this, LoginActivity::class.java))
            finish()
        }
    }
}
