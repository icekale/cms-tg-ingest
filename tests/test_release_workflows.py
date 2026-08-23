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

    def test_release_metadata_describes_multi_directory_resume(self):
        from app import __version__

        self.assertEqual(__version__, "0.4.22")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("多目录", changelog)
        self.assertIn("继续整理", changelog)

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
        self.assertIn("git tag v0.2.90", readme)
        self.assertIn(
            "docker pull icekale/cms-tg-ingest:0.2.48\n# 将 compose 的 image 改为 0.2.48",
            readme,
        )

    def test_dockerhub_overview_contains_complete_compose(self):
        overview = (ROOT / "docs/dockerhub-overview.md").read_text(encoding="utf-8")

        for term in (
            "env_file:",
            "./data:/data",
            "115-cookies.txt:/config/115-cookies.txt:ro",
            "cms-online.db:/cms/cms-online.db:ro",
            "config:/config/cms-config:ro",
            "healthcheck:",
        ):
            self.assertIn(term, overview)
        self.assertRegex(overview, r"image: icekale/cms-tg-ingest:[0-9][^ ]*")

    def test_dockerhub_description_can_sync_without_rebuilding_images(self):
        workflow = (ROOT / ".github/workflows/sync-dockerhub-description.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("DOCKERHUB_USERNAME", workflow)
        self.assertIn("DOCKERHUB_TOKEN", workflow)
        self.assertIn("docs/dockerhub-overview.md", workflow)
        self.assertIn("hub.docker.com/v2/repositories", workflow)


if __name__ == "__main__":
    unittest.main()
