package ai.ravan.assistant;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Registered before the bridge is created so the web layer can call the
        // plugin on its first tick.
        registerPlugin(PhoneActionsPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
