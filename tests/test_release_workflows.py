from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-images.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_publishes_ghcr_and_optional_dockerhub(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("ghcr.io", content)
        self.assertIn("DOCKERHUB_USERNAME", content)
        self.assertIn("DOCKERHUB_TOKEN", content)
        self.assertIn("docker/build-push-action", content)
        self.assertIn("docker/metadata-action", content)
        self.assertIn("type=semver,pattern={{version}}", content)
        self.assertIn("type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}", content)
        self.assertIn("platforms: linux/amd64,linux/arm64", content)

    def test_release_workflow_syncs_dockerhub_description(self):
        content = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("full_description", content)
        self.assertIn("docs/dockerhub-overview.md", content)
        self.assertIn("hub.docker.com/v2/repositories", content)

    def test_readme_documents_release_secrets(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("GHCR", readme)
        self.assertIn("DOCKERHUB_USERNAME", readme)
        self.assertIn("DOCKERHUB_TOKEN", readme)
        self.assertIn("git tag v0.2.17", readme)


if __name__ == "__main__":
    unittest.main()
