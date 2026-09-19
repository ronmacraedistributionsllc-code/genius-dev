# Homebrew formula for the standalone Genius Dev binary (Apple silicon).
# Publish: create a GitHub release "v0.1.0" on the repo with dist/genius-dev-0.1.0-macos-arm64.tar.gz attached,
# put this file in a tap repo (homebrew-tap/Formula/genius-dev.rb), then:  brew install <you>/tap/genius-dev
class GeniusDev < Formula
  desc "Local-first autonomous AI software-engineering CLI/TUI"
  homepage "https://github.com/ronmacraedistributionsllc-code/genius-dev"
  url "https://github.com/ronmacraedistributionsllc-code/genius-dev/releases/download/v0.1.0/genius-dev-0.1.0-macos-arm64.tar.gz"
  sha256 "af6ed54a3e54c270d426cbead472b6071397e96e6e1f2ac0fbea0069719952a1"
  version "0.1.0"
  depends_on arch: :arm64
  depends_on :macos

  def install
    libexec.install Dir["genius/*"]
    bin.install_symlink libexec/"genius"
  end

  def caveats
    <<~EOS
      Genius Dev works offline out of the box (try: genius demo).
      Add a provider key later:  genius models key anthropic
      Project commands (tests, builds) use the python3 on your PATH; browser QA needs: pip install playwright && playwright install chromium
    EOS
  end

  test do
    assert_match "genius-dev", shell_output("#{bin}/genius --version")
  end
end
