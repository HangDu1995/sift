package com.example.medium.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.fragment.app.Fragment
import com.example.medium.R
import com.example.medium.model.User

class HomeFragment : Fragment() {

    private var user: User? = null

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View? {
        return inflater.inflate(R.layout.fragment_home, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        user?.let { u ->
            view.findViewById<TextView>(R.id.tv_welcome).text =
                "Welcome, ${u.displayName}"
        }
    }

    companion object {
        fun newInstance(user: User): HomeFragment {
            return HomeFragment().apply {
                this.user = user
            }
        }
    }
}
