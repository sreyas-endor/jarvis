import SwiftUI

struct ContentView: View {
    @EnvironmentObject var callManager: CallManager
    // Default to localhost so the simulator works out of the box; on a real
    // phone the user puts in the Mac's Tailscale IP (e.g. 100.x.x.x:7860).
    @AppStorage("serverURL") private var serverURL: String = "http://localhost:7860"

    var body: some View {
        VStack(spacing: 24) {
            Text("Jarvis")
                .font(.system(size: 48, weight: .bold))
            Text(callManager.statusText)
                .font(.body)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .frame(minHeight: 40)

            VStack(alignment: .leading, spacing: 8) {
                Text("Mac server URL").font(.caption).foregroundColor(.secondary)
                TextField("http://100.x.x.x:7860", text: $serverURL)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.URL)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                    .disabled(callManager.isInCall)
            }

            if callManager.isInCall {
                Button(role: .destructive) {
                    callManager.endCall()
                } label: {
                    Label("Hang up", systemImage: "phone.down.fill")
                        .font(.title3.bold())
                        .frame(maxWidth: .infinity)
                        .padding()
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
            } else {
                Button {
                    callManager.startCall(serverURL: serverURL)
                } label: {
                    Label("Call Jarvis", systemImage: "phone.fill")
                        .font(.title3.bold())
                        .frame(maxWidth: .infinity)
                        .padding()
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
            }
        }
        .padding(32)
    }
}

#Preview {
    ContentView().environmentObject(CallManager())
}
