# Azure AI Search modules
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchFieldDataType,
    ComplexField,
    SimpleField,
    SearchableField,
    SearchField,
    VectorSearch,
    ExhaustiveKnnAlgorithmConfiguration,
    ExhaustiveKnnParameters,
    VectorSearchProfile,
    SemanticSearch,
    SemanticConfiguration,
    SemanticPrioritizedFields,
    SemanticField,
    HnswAlgorithmConfiguration,
    HnswParameters,
    ScoringProfile,
    TextWeights,
    FreshnessScoringFunction,
    FreshnessScoringParameters
)

from ..ai_surface import Completitions
import json
import os

# Load config file
#with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.json'), 'r') as file:
#    config = json.load(file)
config = {
    "aiSearchEndpoint": "https://search-gio.search.windows.net",
    "indexName": "rfp-ai",
    "semanticConfiguration": "default-rfp-ai",
    "vectorField": "embedding",
    "contentField": "content",
    "tagsField": "tags",
    "sourceField": "source",
    "metadataField": "metadata",
    "embeddingModel": "text-embedding-ada-002",
    "embeddingDimension": 1536
}

AZURE_AI_SEARCH_ENDPOINT = config['aiSearchEndpoint']

## --------------------------------------------- Function to create index ------------------------------------------------
def create_index() -> None:
    """
    Creates an Azure Cognitive Search index with semantic and vector search capabilities.

    The index includes fields for RFP data such as content, questions, tags, and embeddings.
    It also defines scoring profiles and semantic configurations for enhanced search relevance.
    """
    credential = AzureKeyCredential('AZURE_SEARCH_API_KEY_PLACEHOLDER')

    # Analyzer for text fields
    MICROSOFT_ANALYZER = 'en.microsoft'

    index_name = config['indexName']

    # Initialize the SearchIndexClient
    index_client = SearchIndexClient(endpoint=AZURE_AI_SEARCH_ENDPOINT, credential=credential)

    # Delete the existing index if it exists
    try:
        index_client.delete_index(index_name)
        print(f"Deleted existing index: {index_name}")
    except Exception as e:
        print(f"No existing index to delete: {e}")

    # Define the index schema
    index = SearchIndex(
        name=index_name,
        fields=[
            # Unique ID of the chunk
            SimpleField(
                name='id',
                type=SearchFieldDataType.String,
                key=True
            ),
            # Main content of the document
            SearchableField(
                name='content',
                type=SearchFieldDataType.String,
                searchable=True,
                retrievable=True,
                analyzer_name=MICROSOFT_ANALYZER
            ),
            # Question field from the QnA database
            SearchableField(
                name='question',
                type=SearchFieldDataType.String,
                searchable=True,
                retrievable=True,
                filterable=True,
                analyzer_name=MICROSOFT_ANALYZER,
                nullable=True
            ),
            # Section field for categorization
            SearchableField(
                name='section',
                type=SearchFieldDataType.String,
                searchable=True,
                retrievable=True,
                analyzer_name=MICROSOFT_ANALYZER,
                nullable=True
            ),
            # Tag of the QnA entry
            SimpleField(
                name='tags',
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                collection=True,
                retrievable=True,
                filterable=True,
                facetable=True,
                searchable=True,
                analyzer_name='standard.lucene'
            ),
            # Source of the record
            SimpleField(
                name='source',
                type=SearchFieldDataType.String,
                searchable=False,
                retrievable=True,
                filterable=True,
                facetable=True
            ),
            SimpleField(
                name='answer_index',
                type=SearchFieldDataType.Int32,
                searchable=False
            ),
            # Keywords for enhancing retrieval
            SearchableField(
                name='keywords',
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                searchable=True,
                collection=True,
                retrievable=True,
                filterable=True,
                facetable=False,
                analyzer_name=MICROSOFT_ANALYZER
            ),
            # Flag for records that come from "approved sources"
            SimpleField(
                name='approvedSource',
                type=SearchFieldDataType.Boolean,
                filterable=True,
                nullable=True
            ),
            # For temporary records, owner of the record
            SimpleField(
                name='ownersIds',
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                retrievable=True,
                filterable=True,
                nullable=True
            ),
            # Who is the record available to?
            SimpleField(
                name='availableTo',
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                retrievable=True,
                filterable=True,
                nullable=True
            ),
            # For temporary records, update date
            SimpleField(
                name='updateDate',
                type=SearchFieldDataType.DateTimeOffset,
                filterable=True,
                sortable=True,
                retrievable=True,
                nullable=True
            ),
            # Embedding of content for vector search
            SearchField(
                name='embeddingContent',
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                key=False,
                searchable=True,
                vector_search_dimensions=config['embeddingDimension'],
                vector_search_profile_name='embedding_profile'
            ),
            SearchField(
                name='embeddingQuestion',
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                key=False,
                searchable=True,
                vector_search_dimensions=config['embeddingDimension'],
                vector_search_profile_name='embedding_profile'
            ),
            SearchField(
                name='embeddingBlend',
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                key=False,
                searchable=True,
                vector_search_dimensions=config['embeddingDimension'],
                vector_search_profile_name='embedding_profile'
            )
        ],
        # Vector search configuration
        vector_search=VectorSearch(
            algorithms=[
                ExhaustiveKnnAlgorithmConfiguration(
                    name='knn',
                    kind='exhaustiveKnn',
                    parameters=ExhaustiveKnnParameters(metric='cosine')
                ),
                # ADD HNSW for faster searches on large datasets
                HnswAlgorithmConfiguration(
                    name='hnsw_config',
                    kind='hnsw',
                    parameters=HnswParameters(
                        metric='cosine',
                        m=4,                   # Number of bi-directional links
                        ef_construction=400,   # Size of dynamic candidate list for construction
                        ef_search=500          # Size of dynamic candidate list for search
                    )
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name='embedding_profile',
                    algorithm_configuration_name='hnsw_config'
                )
            ]
        ),
        # Semantic search configuration
        semantic_search=SemanticSearch(
            default_configuration_name='default-rfp-ai',
            configurations=[
                SemanticConfiguration(
                    name='default-rfp-ai',
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name='question'),
                        content_fields=[
                            SemanticField(field_name='content'),
                            SemanticField(field_name='section')
                        ],
                        keywords_fields=[
                            SemanticField(field_name='keywords')
                        ]
                    )
                )
            ]
        )
    )

    # Define scoring profiles – ENHANCED
    scoring_profiles = [
        ScoringProfile(
            name='rfp-weighted',
            text_weights=TextWeights(
                weights={
                    'question': 5,    # Increased from 3
                    'content': 2,     # Increased from 1
                    'keywords': 3,    # Added keyword boosting
                    'section': 1
                }
            ),
            # Boost recent content
            functions=[
                FreshnessScoringFunction(
                    field_name='updateDate',
                    boost=2,
                    parameters=FreshnessScoringParameters(
                        boosting_duration='P365D'  # Boost content from last year
                    ),
                    interpolation='linear'
                )
            ],
            function_aggregation='sum'
        ),
        # Profile for exact question matching
        ScoringProfile(
            name='exact-match',
            text_weights=TextWeights(
                weights={
                    'question': 10,
                    'keywords': 5,
                    'content': 1
                }
            )
        ),
        # Add pure vector profile
        ScoringProfile(
            name='pure-vector',
            text_weights=TextWeights(
                weights={
                    'embeddingQuestion': 1
                }
            )
        )
    ]
    index.scoring_profiles = scoring_profiles

    # Set default scoring profile
    index.default_scoring_profile = 'rfp-weighted'

    # Create the index
    try:
        index_client.create_index(index)
        print(f"Index '{index_name}' created successfully.")
    except Exception as e:
        print(f"Failed to create index: {e}")

    return None
