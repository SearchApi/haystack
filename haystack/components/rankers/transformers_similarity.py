import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from haystack import ComponentError, Document, component, default_from_dict, default_to_dict
from haystack.lazy_imports import LazyImport
from haystack.utils import ComponentDevice
from haystack.utils.hf import deserialize_hf_model_kwargs, serialize_hf_model_kwargs

logger = logging.getLogger(__name__)


with LazyImport(message="Run 'pip install transformers[torch,sentencepiece]'") as torch_and_transformers_import:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer


@component
class TransformersSimilarityRanker:
    """
    Ranks Documents based on their similarity to the query.
    It uses a pre-trained cross-encoder model (from the Hugging Face Hub) to embed the query and the Documents.

    Usage example:
    ```
    from haystack import Document
    from haystack.components.rankers import TransformersSimilarityRanker

    ranker = TransformersSimilarityRanker()
    docs = [Document(content="Paris"), Document(content="Berlin")]
    query = "City in Germany"
    output = ranker.run(query=query, documents=docs)
    docs = output["documents"]
    assert len(docs) == 2
    assert docs[0].content == "Berlin"
    ```
    """

    def __init__(
        self,
        model: Union[str, Path] = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        device: Optional[ComponentDevice] = None,
        token: Union[bool, str, None] = None,
        top_k: int = 10,
        query_prefix: str = "",
        document_prefix: str = "",
        meta_fields_to_embed: Optional[List[str]] = None,
        embedding_separator: str = "\n",
        scale_score: bool = True,
        calibration_factor: Optional[float] = 1.0,
        score_threshold: Optional[float] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
    ):
        """
        Creates an instance of TransformersSimilarityRanker.

        :param model: The name or path of a pre-trained cross-encoder model
            from the Hugging Face Hub.
        :param device: The device on which the model is loaded. If `None`, the default device is automatically
            selected.
        :param token: The API token used to download private models from Hugging Face.
            If this parameter is set to `True`, the token generated when running
            `transformers-cli login` (stored in ~/.huggingface) is used.
        :param top_k: The maximum number of Documents to return per query.
        :param query_prefix: A string to add to the beginning of the query text before ranking.
            Can be used to prepend the text with an instruction, as required by some reranking models,
            such as bge.
        :param document_prefix: A string to add to the beginning of each Document text before ranking.
            Can be used to prepend the text with an instruction, as required by some embedding models,
            such as bge.
        :param meta_fields_to_embed: List of meta fields that should be embedded along with the Document content.
        :param embedding_separator: Separator used to concatenate the meta fields to the Document content.
        :param scale_score: Whether the raw logit predictions will be scaled using a Sigmoid activation function.
            Set this to False if you do not want any scaling of the raw logit predictions.
        :param calibration_factor: Factor used for calibrating probabilities calculated by
            `sigmoid(logits * calibration_factor)`. This is only used if `scale_score` is set to True.
        :param score_threshold: If provided only returns documents with a score above this threshold.
        :param model_kwargs: Additional keyword arguments passed to `AutoModelForSequenceClassification.from_pretrained`
            when loading the model specified in `model`. For details on what kwargs you can pass,
            see the model's documentation.
        """
        torch_and_transformers_import.check()

        self.model = model
        if top_k <= 0:
            raise ValueError(f"top_k must be > 0, but got {top_k}")
        self.top_k = top_k
        self.device = ComponentDevice.resolve_device(device)
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.token = token
        self._model = None
        self.tokenizer = None
        self.meta_fields_to_embed = meta_fields_to_embed or []
        self.embedding_separator = embedding_separator
        self.scale_score = scale_score
        self.calibration_factor = calibration_factor
        if self.scale_score and self.calibration_factor is None:
            raise ValueError(
                f"scale_score is True so calibration_factor must be provided, but got {calibration_factor}"
            )
        self.score_threshold = score_threshold
        self.model_kwargs = model_kwargs or {}

        if self.device.has_multiple_devices:
            raise ValueError(
                f"{type(TransformersSimilarityRanker).__name__} currently only supports inference on single devices"
            )

    def _get_telemetry_data(self) -> Dict[str, Any]:
        """
        Data that is sent to Posthog for usage analytics.
        """
        return {"model": str(self.model)}

    def warm_up(self):
        """
        Warm up the model and tokenizer used for scoring the Documents.
        """
        if self._model is None:
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.model, token=self.token, **self.model_kwargs
            ).to(self.device.to_torch())
            self._model.eval()
            self.tokenizer = AutoTokenizer.from_pretrained(self.model, token=self.token)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize this component to a dictionary.
        """
        serialization_dict = default_to_dict(
            self,
            device=self.device.to_dict(),
            model=self.model,
            token=self.token if not isinstance(self.token, str) else None,  # don't serialize valid tokens
            top_k=self.top_k,
            query_prefix=self.query_prefix,
            document_prefix=self.document_prefix,
            meta_fields_to_embed=self.meta_fields_to_embed,
            embedding_separator=self.embedding_separator,
            scale_score=self.scale_score,
            calibration_factor=self.calibration_factor,
            score_threshold=self.score_threshold,
            model_kwargs=self.model_kwargs,
        )

        serialize_hf_model_kwargs(serialization_dict["init_parameters"]["model_kwargs"])
        return serialization_dict

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransformersSimilarityRanker":
        """
        Deserialize this component from a dictionary.
        """
        init_params = data["init_parameters"]
        init_params["device"] = ComponentDevice.from_dict(init_params["device"])
        deserialize_hf_model_kwargs(init_params["model_kwargs"])

        return default_from_dict(cls, data)

    @component.output_types(documents=List[Document])
    def run(
        self,
        query: str,
        documents: List[Document],
        top_k: Optional[int] = None,
        scale_score: Optional[bool] = None,
        calibration_factor: Optional[float] = None,
        score_threshold: Optional[float] = None,
    ):
        """
        Returns a list of Documents ranked by their similarity to the given query.

        :param query: Query string.
        :param documents: List of Documents.
        :param top_k: The maximum number of Documents you want the Ranker to return.
        :param scale_score: Whether the raw logit predictions will be scaled using a Sigmoid activation function.
            Set this to False if you do not want any scaling of the raw logit predictions.
        :param calibration_factor: Factor used for calibrating probabilities calculated by
            `sigmoid(logits * calibration_factor)`. This is only used if `scale_score` is set to True.
        :param score_threshold: If provided only returns documents with a score above this threshold.
        :return: List of Documents sorted by their similarity to the query with the most similar Documents appearing first.
        """
        if not documents:
            return {"documents": []}

        top_k = top_k or self.top_k
        if top_k <= 0:
            raise ValueError(f"top_k must be > 0, but got {top_k}")

        scale_score = scale_score or self.scale_score
        calibration_factor = calibration_factor or self.calibration_factor

        if scale_score and calibration_factor is None:
            raise ValueError(
                f"scale_score is True so calibration_factor must be provided, but got {calibration_factor}"
            )

        if score_threshold is None:
            score_threshold = self.score_threshold

        # If a model path is provided but the model isn't loaded
        if self.model and not self._model:
            raise ComponentError(
                f"The component {self.__class__.__name__} wasn't warmed up. Run 'warm_up()' before calling 'run()'."
            )

        query_doc_pairs = []
        for doc in documents:
            meta_values_to_embed = [
                str(doc.meta[key]) for key in self.meta_fields_to_embed if key in doc.meta and doc.meta[key]
            ]
            text_to_embed = self.embedding_separator.join(meta_values_to_embed + [doc.content or ""])
            query_doc_pairs.append([self.query_prefix + query, self.document_prefix + text_to_embed])

        features = self.tokenizer(
            query_doc_pairs, padding=True, truncation=True, return_tensors="pt"
        ).to(  # type: ignore
            self.device.to_torch()
        )
        with torch.inference_mode():
            similarity_scores = self._model(**features).logits.squeeze(dim=1)  # type: ignore

        if scale_score:
            similarity_scores = torch.sigmoid(similarity_scores * calibration_factor)

        _, sorted_indices = torch.sort(similarity_scores, descending=True)

        sorted_indices = sorted_indices.cpu().tolist()  # type: ignore
        similarity_scores = similarity_scores.cpu().tolist()
        ranked_docs = []
        for sorted_index in sorted_indices:
            i = sorted_index
            documents[i].score = similarity_scores[i]
            ranked_docs.append(documents[i])

        if score_threshold is not None:
            ranked_docs = [doc for doc in ranked_docs if doc.score >= score_threshold]

        return {"documents": ranked_docs[:top_k]}
